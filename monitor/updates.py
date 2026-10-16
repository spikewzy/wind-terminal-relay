"""Gitee version checks and explicitly requested Windows upgrades. No Wind calls."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

try:
    from .diagnostics import VERSION
    from .management import ManagementError, safe_path, read, write, protect
except ImportError:
    from diagnostics import VERSION
    from management import ManagementError, safe_path, read, write, protect

BASE = 'https://gitee.com/spikewangzy/wind-terminal-relay/raw/main/'
REPOSITORY_PATH = '/spikewangzy/wind-terminal-relay/raw/main/'
FEED = BASE + 'updates/stable.json'
CHECK_INTERVAL = 6 * 3600
MAX_PACKAGE = 10 * 1024 * 1024
SEMVER = re.compile(r'(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
OPERATION = re.compile(r'[a-f0-9]{32}\Z')
ACTIVE = {'downloading', 'verifying', 'launching', 'installing'}
STATES = ACTIVE | {'succeeded', 'failed', 'interrupted'}
HERE = Path(__file__).absolute().parent
CA_SHA256 = '9cc2a774b5198dcff14d9be1e66091f538975d867ce029a96bce15a55dfd730f'


class UpdateError(ManagementError):
    pass


def private_directory(path, exist_ok=False):
    path = safe_path(path)
    path.mkdir(parents=True, exist_ok=exist_ok, mode=0o700)
    protect(path)
    if os.name != 'nt':
        path.chmod(0o700)
    return path


def version(value):
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise UpdateError('UPDATE_FEED_INVALID', 502)
    return tuple(map(int, value.split('.')))


def trusted_url(url):
    # Feed and archives use the same mirrored repository. No GitHub fallback,
    # credentials, arbitrary URL input, unrelated redirects or local requests.
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parts.path)
        if (parts.scheme != 'https' or parts.hostname not in {'gitee.com', 'raw.giteeusercontent.com'} or
                parts.port not in {None,443} or parts.username or parts.password or parts.fragment or
                not path.startswith(REPOSITORY_PATH) or
                not re.fullmatch(r'[A-Za-z0-9_./-]+',path) or
                any(p in {'','.','..'} for p in path[len(REPOSITORY_PATH):].split('/'))):
            return False
        if parts.hostname == 'gitee.com':
            return not parts.query
        # Gitee currently redirects raw files to its signed content host.
        # These are public download signatures; no relay or console keys leave.
        query = urllib.parse.parse_qs(parts.query, strict_parsing=True)
        return (set(query) == {'metadata','signature'} and
                all(len(v)==1 and re.fullmatch(r'[A-Za-z0-9_=-]{1,8192}',v[0]) for v in query.values()))
    except ValueError:
        return False


class RepositoryRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted_url(newurl):
            raise UpdateError('UPDATE_REDIRECT_DENIED', 502)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def tls_context(bundle=None, expected_sha256=CA_SHA256, context_factory=ssl.create_default_context):
    """Retain system trust and add the pinned public Mozilla/certifi CA set.

    Windows SYSTEM can have a different/older default trust store than the
    desktop browser. No Windows certificate store is modified, and chain,
    hostname and the runtime's strict verification flags remain enabled.
    """
    try:
        data = safe_path(HERE/'cacert.pem' if bundle is None else bundle).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError()
        context = context_factory()
        context.load_verify_locations(cadata=data.decode('ascii'))
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError()
        return context
    except (OSError, ValueError, UnicodeError):
        raise UpdateError('UPDATE_CA_BUNDLE_INVALID', 500) from None


def failure_code(exc, stage='check'):
    if isinstance(exc, ManagementError):
        return exc.code
    cause = getattr(exc, 'reason', None)
    if not isinstance(cause, BaseException):
        cause = exc
    if isinstance(cause, ssl.SSLCertVerificationError):
        code = getattr(cause, 'verify_code', None)
        return ('UPDATE_TLS_CERT_EXPIRED' if code == 10 else
                'UPDATE_TLS_CERT_NOT_YET_VALID' if code == 9 else
                'UPDATE_TLS_HOSTNAME_MISMATCH' if code in {62,64} else 'UPDATE_TLS_CERTIFICATE_ERROR')
    if isinstance(cause, ssl.SSLError):
        return 'UPDATE_TLS_HANDSHAKE_ERROR'
    if isinstance(cause, socket.gaierror):
        return 'UPDATE_DNS_ERROR'
    if isinstance(cause, TimeoutError):
        return 'UPDATE_TIMEOUT'
    if isinstance(exc, urllib.error.HTTPError):
        return ('UPDATE_FEED_NOT_FOUND' if exc.code == 404 and stage == 'check' else
                'UPDATE_PACKAGE_NOT_FOUND' if exc.code == 404 else
                'UPDATE_SOURCE_FORBIDDEN' if exc.code in {401,403} else
                'UPDATE_RATE_LIMITED' if exc.code == 429 else 'UPDATE_HTTP_ERROR')
    return 'UPDATE_SOURCE_UNAVAILABLE' if stage == 'check' else 'UPDATE_PREPARATION_FAILED'


def fetch(url, maximum):
    if not trusted_url(url):
        raise UpdateError('UPDATE_SOURCE_DENIED', 400)
    request = urllib.request.Request(url, headers={
        'User-Agent': 'WindRelay-Update/' + VERSION, 'Cache-Control': 'no-cache'})
    opener = urllib.request.build_opener(RepositoryRedirects(), urllib.request.HTTPSHandler(context=tls_context()))
    with opener.open(request, timeout=20) as response:
        if not trusted_url(response.geturl()):
            raise UpdateError('UPDATE_SOURCE_DENIED', 502)
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise UpdateError('UPDATE_DOWNLOAD_TOO_LARGE', 502)
    return data


def validate_feed(data):
    try:
        value = json.loads(data.decode('utf-8-sig'))
        if (not isinstance(value, dict) or value.get('schema_version') != 1 or
                value.get('channel') != 'stable' or
                not isinstance(value.get('releases'), list) or not 1 <= len(value['releases']) <= 10):
            raise ValueError()
        releases = []
        seen = set()
        for item in value['releases']:
            gateway, console = item['gateway_version'], item['console_version']
            version(gateway); version(console)
            if gateway in seen or item.get('updater_protocol') != 1:
                raise ValueError()
            seen.add(gateway)
            name = 'wind-relay-gateway-upgrade-' + gateway + '.zip'
            path = 'updates/packages/' + name
            if (item['path'] != path or not SHA.fullmatch(item['sha256']) or
                    not SHA.fullmatch(item['build_id']) or type(item['bytes']) is not int or
                    not 0 < item['bytes'] <= MAX_PACKAGE or
                    not re.fullmatch(r'20[0-9]{2}-[0-9]{2}-[0-9]{2}', item['published_on']) or
                    not isinstance(item['notes'], str) or len(item['notes']) > 1500):
                raise ValueError()
            releases.append({key: item[key] for key in (
                'gateway_version', 'console_version', 'path', 'sha256', 'build_id',
                'bytes', 'published_on', 'notes', 'updater_protocol')})
        return sorted(releases, key=lambda r: version(r['gateway_version']), reverse=True)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise UpdateError('UPDATE_FEED_INVALID', 502) from None


def extract_package(data, release, destination):
    """Check the mirrored ZIP, all paths, then every manifested member before use."""
    if len(data) != release['bytes'] or hashlib.sha256(data).hexdigest() != release['sha256']:
        raise UpdateError('UPDATE_HASH_MISMATCH', 502)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if (not 1 <= len(members) <= 150 or len({n.casefold() for n in names}) != len(names) or
                    sum(item.file_size for item in members) > 8 * 1024 * 1024):
                raise ValueError()
            for item in members:
                name = item.filename
                mode = item.external_attr >> 16
                if (not re.fullmatch(r'[A-Za-z0-9_./-]+', name) or name.startswith('/') or
                        any(part in {'', '.', '..'} or part.endswith(('.', ' ')) for part in name.split('/')) or
                        item.is_dir() or item.flag_bits & 1 or stat.S_ISLNK(mode) or
                        stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
                    raise ValueError()
            manifest = json.loads(archive.read('upgrade-manifest.json'))
            if (manifest.get('kind') != 'wind_relay_gateway_upgrade' or
                    manifest.get('version') != release['gateway_version'] or
                    manifest.get('build_id') != release['build_id'] or
                    set(manifest['files']) != set(names) - {'upgrade-manifest.json'} or
                    'Upgrade-Gateway.ps1' not in names):
                raise ValueError()
            console = json.loads(archive.read('console-manifest.json'))
            if console.get('version') != release['console_version']:
                raise ValueError()
            for name, expected in manifest['files'].items():
                if not isinstance(expected, str) or not SHA.fullmatch(expected) or hashlib.sha256(archive.read(name)).hexdigest() != expected:
                    raise ValueError()
            destination = private_directory(destination)
            for name in names:
                target = safe_path(destination/name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as stream:
                    protect(target)
                    stream.write(archive.read(name))
    except (zipfile.BadZipFile, ValueError, KeyError, TypeError, UnicodeError):
        raise UpdateError('UPDATE_PACKAGE_INVALID', 502) from None


def launch(app, directory, operation):
    if os.name != 'nt':
        raise UpdateError('UPDATE_WINDOWS_ONLY', 409)
    # Copy the installed runner outside the console task's directory/cgroup;
    # the console may be restarted while this independent scheduled task runs.
    for name in ('Run-OnlineUpgrade.ps1', 'Launch-Upgrade.ps1'):
        target = safe_path(directory/name)
        with target.open('xb') as stream:
            protect(target)
            stream.write(safe_path(HERE/name).read_bytes())
    result = subprocess.run([
        'powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', str(directory/'Launch-Upgrade.ps1'), '-AppDirectory', str(app),
        '-OperationDirectory', str(directory), '-OperationId', operation,
        '-PythonExe', sys.executable], capture_output=True, timeout=40)
    if result.returncode:
        raise UpdateError('UPDATE_LAUNCH_FAILED', 500)


def task_running():
    if os.name != 'nt':
        return False
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            "(Get-ScheduledTask -TaskName WindRelayUpgrade -ErrorAction SilentlyContinue).State"],
            capture_output=True, timeout=10)
        return result.returncode == 0 and result.stdout.strip() == b'Running'
    except (OSError, subprocess.TimeoutExpired):
        return None


class Updates:
    def __init__(self, app, guard, current, recorder, *, fetcher=fetch, launcher=launch,
                 running=task_running, windows=None):
        self.app = safe_path(app)
        self.root = self.app/'.relay/online-updates'
        self.guard, self.current, self.recorder = guard, current, recorder
        self.fetcher, self.launcher, self.running = fetcher, launcher, running
        self.windows = os.name == 'nt' if windows is None else windows
        self.lock = threading.RLock()
        self.releases = []
        self.checked_at = None
        self.last_attempt_at = None
        self.check_error = None
        self.checking = False
        self.next_check = 0
        self.worker = None

    def check(self):
        with self.lock:
            if self.checking:
                return
            self.checking = True
            self.last_attempt_at = time.time()
            self.next_check = time.time() + CHECK_INTERVAL
        def work():
            try:
                releases = validate_feed(self.fetcher(FEED, 64 * 1024))
                with self.lock:
                    self.releases, self.checked_at, self.check_error = releases, time.time(), None
                self.recorder.record('update_checked', 'updates', 'UPDATE_CHECK_OK')
            except Exception as exc:
                with self.lock:
                    self.check_error = failure_code(exc)
                self.recorder.record('update_check_failed', 'updates', self.check_error, exc=exc)
            finally:
                with self.lock:
                    self.checking = False
        threading.Thread(target=work, daemon=True).start()

    def tick(self):
        if time.time() >= self.next_check:
            self.check()

    def eligible(self, release):
        current = self.current()
        old_gateway = current.get('gateway_version')
        if old_gateway is not None:
            if version(release['gateway_version']) < version(old_gateway):
                return False
        if version(release['console_version']) < version(VERSION):
            return False
        return old_gateway is None or (version(release['gateway_version']) > version(old_gateway) or
                                      version(release['console_version']) > version(VERSION))

    def operation(self):
        path = safe_path(self.root/'current.json')
        if not path.exists():
            return None
        value = read(path)
        identifier = value.get('operation_id')
        if not isinstance(identifier, str) or not OPERATION.fullmatch(identifier) or value.get('state') not in STATES:
            raise UpdateError('UPDATE_STATE_INVALID')
        result = safe_path(self.root/identifier/'result.json')
        if result.exists():
            result_value = read(result)
            if result_value.get('operation_id') == identifier and result_value.get('state') in {'succeeded', 'failed'}:
                value['state'] = result_value['state']
                value['error_code'] = ('UPDATE_RUNTIME_CHECK_FAILED' if value['state'] == 'failed' else None)
        elif value['state'] in ACTIVE and not (self.worker and self.worker.is_alive()) and time.time() - value['updated_at'] > 60:
            running = self.running()
            if running is False:
                value['state'], value['error_code'] = 'interrupted', 'UPDATE_INTERRUPTED'
                # Never restart or replay an interrupted upgrade automatically.
        return {key: value.get(key) for key in ('operation_id', 'state', 'gateway_version', 'console_version',
                                               'started_at', 'updated_at', 'error_code')}

    def status(self):
        with self.lock:
            return {'source': 'gitee', 'check_interval_hours': 6, 'checking': self.checking,
                'checked_at': self.checked_at, 'last_attempt_at': self.last_attempt_at, 'check_error': self.check_error,
                'automatic_install': False, 'windows_supported': self.windows,
                'current': dict(self.current(), console_version=VERSION),
                'releases': [dict(r, eligible=self.eligible(r)) for r in self.releases],
                'operation': self.operation()}

    def install(self, gateway_version, confirmed):
        with self.lock:
            if confirmed is not True:
                raise UpdateError('UPDATE_CONFIRMATION_REQUIRED', 400)
            if not self.windows:
                raise UpdateError('UPDATE_WINDOWS_ONLY')
            old = self.operation()
            if old and old['state'] in ACTIVE:
                raise UpdateError('UPDATE_ALREADY_RUNNING')
            if self.running() is True:
                raise UpdateError('UPDATE_ALREADY_RUNNING')
            if self.check_error or not self.checked_at or time.time() - self.checked_at > CHECK_INTERVAL:
                raise UpdateError('UPDATE_CHECK_REQUIRED')
            release = next((r for r in self.releases if r['gateway_version'] == gateway_version), None)
            if release is None or not self.eligible(release):
                raise UpdateError('UPDATE_VERSION_NOT_AVAILABLE')
            self.guard()
            private_directory(self.root, exist_ok=True)
            identifier = uuid.uuid4().hex
            directory = private_directory(self.root/identifier)
            value = {'operation_id': identifier, 'state': 'downloading',
                     'gateway_version': release['gateway_version'], 'console_version': release['console_version'],
                     'started_at': time.time(), 'updated_at': time.time(), 'error_code': None}
            write(self.root/'current.json', value)
            write(directory/'selection.json', dict(value, app_directory=str(self.app),
                                                  package_sha256=release['sha256'], build_id=release['build_id']))
            self.worker = threading.Thread(target=self._install, args=(release, directory, value), daemon=True)
            self.worker.start()
            return dict(value)

    def _stage(self, value, state, error=None):
        with self.lock:
            value.update(state=state, error_code=error, updated_at=time.time())
            write(self.root/'current.json', value)

    def _install(self, release, directory, value):
        try:
            data = self.fetcher(BASE + release['path'], release['bytes'])
            self._stage(value, 'verifying')
            extract_package(data, release, directory/'package')
            # Recheck idle and pending config changes after a possibly slow download.
            self.guard()
            self._stage(value, 'launching')
            self.launcher(self.app, directory, value['operation_id'])
            self._stage(value, 'installing')
            self.recorder.record('update_launched', 'updates', 'ONLINE_UPGRADE_STARTED')
        except Exception as exc:
            code = failure_code(exc, 'install')
            self._stage(value, 'failed', code)
            self.recorder.record('update_failed', 'updates', code, exc=exc)
