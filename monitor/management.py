"""Local administration only. No Wind imports, query submission or DB writes."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")


class ManagementError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def safe_path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (path, *path.parents)):
        raise ManagementError('UNSAFE_PATH')
    return path


def read(path):
    return json.loads(safe_path(path).read_text(encoding='utf-8-sig'))


def protect(path):
    os.chmod(path, 0o600)
    if os.name == 'nt':
        # chmod alone does not set a private Windows ACL. Console writes are
        # restricted to SYSTEM and Administrators, including config backups.
        import ctypes
        from ctypes import wintypes
        descriptor = ctypes.c_void_p()
        advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:P(A;;FA;;;SY)(A;;FA;;;BA)', 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(descriptor)


def write(path, value, exclusive=False):
    """Private atomic replacement; new credential files are never overwritten."""
    path = safe_path(path)
    data = (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode()
    if exclusive:
        with path.open('xb') as stream:
            protect(path)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    fd, name = tempfile.mkstemp(prefix='.console-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            protect(name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def config_lock(config):
    # Same file and lock range as relay_setup.py, also on Windows.
    path = safe_path(str(config)+'.admin.lock')
    with path.open('a+b') as stream:
        os.chmod(path, 0o600)
        if stream.tell() == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ManagementError('CONFIG_BUSY') from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ManagementError('CONFIG_BUSY') from None
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def access_key(path):
    key = safe_path(path).read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', key):
        raise ValueError('Invalid console key')
    return key


class Management:
    def __init__(self, config, client, snapshot, recorder, gateway_port=8765):
        self.config = safe_path(config)
        self.client = safe_path(client)
        self.snapshot = snapshot
        self.recorder = recorder
        self.port = gateway_port
        self.state = self.config.parent/'console-pending.json'

    def gateway(self):
        value = read(self.config)
        clients = value.get('clients')
        if (not isinstance(clients, dict) or not clients or
                any(not ID.fullmatch(k) or not isinstance(v, str) or not HASH.fullmatch(v)
                    for k, v in clients.items())):
            raise ManagementError('CONFIG_INVALID')
        retired = value.get('retired_clients', [])
        if not isinstance(retired, list) or any(not isinstance(k, str) or not ID.fullmatch(k) for k in retired):
            raise ManagementError('CONFIG_INVALID')
        return value

    def owner(self, gateway):
        value = read(self.client)
        token = value.get('token')
        if not isinstance(token, str) or not token or len(token) > 1024:
            raise ManagementError('OWNER_CONFIG_INVALID')
        protected = {key for key, hashed in gateway['clients'].items() if hmac.compare_digest(hashed, digest(token))}
        if not protected:
            raise ManagementError('OWNER_CONFIG_INVALID')
        url = value.get('url', '')
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or
                parts.password or parts.query or parts.fragment or parts.path not in {'', '/'} or
                (parts.scheme == 'http' and parts.hostname not in {'127.0.0.1', 'localhost', '::1'})):
            raise ManagementError('OWNER_CONFIG_INVALID')
        return value, protected

    def pending(self):
        if not safe_path(self.state).exists():
            return []
        value = read(self.state)
        if not isinstance(value, list) or len(value) > 1000:
            raise ManagementError('PENDING_STATE_INVALID')
        return value

    def credential(self, client_id, expected):
        value = read(self.config.parent/('client-'+client_id+'.json'))
        if not isinstance(value.get('token'), str) or not hmac.compare_digest(digest(value['token']), expected):
            raise ManagementError('CLIENT_FILE_MISMATCH')
        owner, _ = self.owner(self.gateway())
        if value.get('url', '').rstrip('/') != owner['url'].rstrip('/'):
            raise ManagementError('CLIENT_FILE_MISMATCH')
        # Strict output schema prevents accidental export of future private fields.
        return {key: value[key] for key in ('url', 'token', 'wait_seconds', 'allow_private_http') if key in value}

    def listing(self):
        gateway = self.gateway()
        _, protected = self.owner(gateway)
        pending = self.pending()
        rows = []
        for key in sorted(set(gateway['clients']) | set(gateway.get('retired_clients', []))):
            active = key in gateway['clients']
            available = False
            if active:
                try:
                    self.credential(key, gateway['clients'][key])
                    available = True
                except (OSError, ValueError, ManagementError):
                    pass
            rows.append({'client_id': key, 'active': active, 'protected': key in protected,
                         'download_available': available})
        return {'clients': rows, 'restart_required': bool(pending), 'pending_changes': len(pending)}

    def idle(self):
        try:
            value = self.snapshot()
            if not value.get('database_ok', False):
                raise ManagementError('DATABASE_UNAVAILABLE')
            if any(value['pending'].get(k, 0) for k in ('queued', 'running', 'uncertain', 'expired_wait')):
                raise ManagementError('TASKS_NOT_IDLE')
        except (OSError, ValueError, KeyError):
            raise ManagementError('DATABASE_UNAVAILABLE') from None

    def change(self, action, client_id, confirmation=None):
        if not isinstance(client_id, str) or not ID.fullmatch(client_id):
            raise ManagementError('INVALID_CLIENT_ID', 400)
        with config_lock(self.config):
            gateway = self.gateway()
            owner, protected = self.owner(gateway)
            self.idle()
            if action == 'add':
                if client_id in gateway['clients'] or client_id in gateway.get('retired_clients', []):
                    raise ManagementError('CLIENT_ID_USED')
                output = safe_path(self.config.parent/('client-'+client_id+'.json'))
                if output.exists():
                    raise ManagementError('CLIENT_FILE_EXISTS')
            elif action == 'revoke':
                if confirmation != client_id:
                    raise ManagementError('CONFIRMATION_REQUIRED', 400)
                if client_id in protected:
                    raise ManagementError('PROTECTED_CLIENT')
                if client_id not in gateway['clients']:
                    raise ManagementError('CLIENT_NOT_FOUND', 404)
            else:
                raise ManagementError('INVALID_ACTION', 400)
            backup_dir = safe_path(self.config.parent/'console-backups')
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            os.chmod(backup_dir, 0o700)
            write(backup_dir/('gateway-'+str(time.time_ns())+'-'+secrets.token_hex(3)+'.json'), gateway, exclusive=True)
            pending = self.pending()
            if action == 'add':
                token = secrets.token_urlsafe(32)
                write(output, {'url': owner['url'], 'token': token, 'wait_seconds': 2,
                               'allow_private_http': owner.get('allow_private_http', False)}, exclusive=True)
                hashed = digest(token)
                gateway['clients'][client_id] = hashed
            else:
                hashed = gateway['clients'].pop(client_id)
                gateway.setdefault('retired_clients', []).append(client_id)
            # Persist reload evidence before configuration; interruption can only
            # leave a conservative pending warning, never falsely claim applied.
            pending.append({'client_id': client_id, 'action': action, 'sha256': hashed})
            write(self.state, pending)
            write(self.config, gateway)
        self.recorder.record('device_configuration_changed', 'management', 'CLIENT_ADDED' if action == 'add' else 'CLIENT_REVOKED')
        return {'client_id': client_id, 'restart_required': True, 'saved': True}

    def download(self, client_id):
        if not ID.fullmatch(client_id):
            raise ManagementError('INVALID_CLIENT_ID', 400)
        gateway = self.gateway()
        if client_id not in gateway['clients']:
            raise ManagementError('CLIENT_NOT_FOUND', 404)
        value = self.credential(client_id, gateway['clients'][client_id])
        self.recorder.record('device_configuration_exported', 'management', 'CLIENT_CONFIG_EXPORTED')
        return (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode()

    def verify_reload(self):
        """Check only authentication on localhost. Never start/retry a Wind job."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        with config_lock(self.config):
            gateway = self.gateway()
            owner, _ = self.owner(gateway)
            pending = self.pending()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            def code(token):
                request = urllib.request.Request('http://127.0.0.1:'+str(self.port)+'/v1/status',
                                                 headers={'Authorization': 'Bearer '+token})
                try:
                    with opener.open(request, timeout=3) as response:
                        return response.status
                except urllib.error.HTTPError as error:
                    return error.code
            if code(owner['token']) != 200:
                raise ManagementError('RELOAD_NOT_CONFIRMED')
            for item in pending:
                key = item.get('client_id', '')
                if not isinstance(key, str) or not ID.fullmatch(key):
                    raise ManagementError('PENDING_STATE_INVALID')
                # When the same device was added then revoked before a reload,
                # its final configured state is what the gateway must enforce.
                value = read(self.config.parent/('client-'+key+'.json'))
                if digest(value.get('token', '')) != item.get('sha256'):
                    raise ManagementError('CLIENT_FILE_MISMATCH')
                if code(value['token']) != (200 if key in gateway['clients'] else 401):
                    raise ManagementError('RELOAD_NOT_CONFIRMED')
            write(self.state, [])
        self.recorder.record('gateway_reload_confirmed', 'management', 'RELOAD_CONFIRMED')
        return {'restart_required': False, 'authentication_verified': True}
