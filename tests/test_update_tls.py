"""Public CA packaging and strict HTTPS verification; no Wind or real secrets."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from monitor.diagnostics import Recorder, error_details, clean_error
from monitor.updates import tls_context, CA_SHA256, HERE, failure_code, UpdateError, Updates


class CertificateTests(unittest.TestCase):
    def test_pinned_public_bundle_preserves_certificate_hostname_and_strict_checks(self):
        provenance=json.loads((HERE/'ca-provenance.json').read_text())
        self.assertEqual(provenance['sha256'],CA_SHA256)
        self.assertEqual(hashlib.sha256((HERE/'cacert.pem').read_bytes()).hexdigest(),CA_SHA256)
        expected=ssl.create_default_context()
        context=tls_context()
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_flags,expected.verify_flags)
        self.assertGreater(context.cert_store_stats()['x509_ca'],50)
        self.assertIn('Mozilla Public License',(HERE/'CA-LICENSE.txt').read_text())

    def test_tampered_missing_or_unverified_context_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory).resolve()/'cacert.pem'
            p.write_bytes(b'TAMPERED')
            with self.assertRaisesRegex(UpdateError,'CA_BUNDLE_INVALID'):tls_context(p)
            with self.assertRaisesRegex(UpdateError,'CA_BUNDLE_INVALID'):tls_context(p.with_name('missing'))
            unsafe=lambda:ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            with self.assertRaisesRegex(UpdateError,'CA_BUNDLE_INVALID'):tls_context(context_factory=unsafe)

    def test_errors_distinguish_tls_dns_timeout_and_http_without_private_message(self):
        for verify,expected in ((20,'UPDATE_TLS_CERTIFICATE_ERROR'),(10,'UPDATE_TLS_CERT_EXPIRED'),
                                (9,'UPDATE_TLS_CERT_NOT_YET_VALID'),(62,'UPDATE_TLS_HOSTNAME_MISMATCH')):
            error=ssl.SSLCertVerificationError(1,'PRIVATE_CERTIFICATE_MESSAGE')
            error.verify_code=verify
            wrapped=urllib.error.URLError(error)
            self.assertEqual(failure_code(wrapped),expected)
            self.assertEqual(failure_code(error),expected)
            details=clean_error(error_details(wrapped))
            self.assertEqual(details['verify_code'],verify)
            self.assertNotIn('PRIVATE_CERTIFICATE',json.dumps(details))
        self.assertEqual(failure_code(urllib.error.URLError(socket.gaierror(-2,'PRIVATE_DNS'))),'UPDATE_DNS_ERROR')
        self.assertEqual(failure_code(urllib.error.URLError(TimeoutError('PRIVATE_TIMEOUT'))),'UPDATE_TIMEOUT')
        for status,expected in ((404,'UPDATE_FEED_NOT_FOUND'),(403,'UPDATE_SOURCE_FORBIDDEN'),(429,'UPDATE_RATE_LIMITED')):
            error=urllib.error.HTTPError('https://private.invalid',status,'PRIVATE_HTTP',{},None)
            self.assertEqual(failure_code(error),expected)
            self.assertEqual(error_details(error)['http_status'],status)
        self.assertEqual(failure_code(urllib.error.HTTPError('https://example.invalid',404,'x',{},None),'install'),'UPDATE_PACKAGE_NOT_FOUND')

    def test_failed_attempt_time_and_certificate_reason_survive_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();recorder=Recorder()
            error=ssl.SSLCertVerificationError(1,'PRIVATE_CERT_INFO');error.verify_code=20
            def fail(*args):raise urllib.error.URLError(error)
            manager=Updates(root,lambda:None,lambda:{},recorder,fetcher=fail)
            try:
                manager.check()
                import time
                deadline=time.monotonic()+3
                while manager.checking and time.monotonic()<deadline:time.sleep(.01)
                state=manager.status()
                self.assertIsNotNone(state['last_attempt_at'])
                self.assertIsNone(state['checked_at'])
                self.assertEqual(state['check_error'],'UPDATE_TLS_CERTIFICATE_ERROR')
                event=recorder.history()[-1]
                self.assertEqual(event['error']['verify_code'],20)
                self.assertNotIn('PRIVATE_CERT_INFO',json.dumps(event))
            finally:recorder.close()

    @unittest.skipUnless(shutil.which('openssl'),'OpenSSL CLI is needed only to generate ephemeral synthetic certificates')
    def test_real_tls_handshake_needs_trusted_ca_and_still_rejects_wrong_hostname(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            def openssl(*args):
                subprocess.run([shutil.which('openssl'),*args],cwd=root,check=True,capture_output=True,timeout=20)
            (root/'ca.cnf').write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ca_extensions\n[dn]\nCN=Relay Synthetic Test CA\n[ca_extensions]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n')
            openssl('req','-x509','-newkey','rsa:2048','-nodes','-days','2','-sha256',
                    '-keyout','ca.key','-out','ca.pem','-config','ca.cnf')
            openssl('req','-newkey','rsa:2048','-nodes','-keyout','server.key','-out','server.csr','-subj','/CN=localhost')
            (root/'server.ext').write_text('subjectAltName=DNS:localhost\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n')
            openssl('x509','-req','-in','server.csr','-CA','ca.pem','-CAkey','ca.key','-CAcreateserial',
                    '-out','server.pem','-days','2','-sha256','-extfile','server.ext')
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):
                    self.send_response(200);self.end_headers();self.wfile.write(b'SYNTHETIC_TLS_OK')
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(root/'server.pem',root/'server.key')
            server.socket=server_context.wrap_socket(server.socket,server_side=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                url='https://localhost:'+str(server.server_port)
                with self.assertRaises(urllib.error.URLError):urllib.request.urlopen(url,context=ssl.create_default_context(),timeout=3)
                expected=hashlib.sha256((root/'ca.pem').read_bytes()).hexdigest()
                context=tls_context(root/'ca.pem',expected)
                with urllib.request.urlopen(url,context=context,timeout=3) as response:self.assertEqual(response.read(),b'SYNTHETIC_TLS_OK')
                with self.assertRaises(urllib.error.URLError) as raised:
                    urllib.request.urlopen('https://127.0.0.1:'+str(server.server_port),context=context,timeout=3)
                self.assertEqual(failure_code(raised.exception),'UPDATE_TLS_HOSTNAME_MISMATCH')
            finally:server.shutdown();server.server_close();thread.join()
