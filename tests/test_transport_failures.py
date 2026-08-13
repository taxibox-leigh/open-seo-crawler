"""Rules that need the transport to misbehave.

These were excused in the coverage harness because a well-behaved static
fixture cannot reach them: TLS failures, hangs, dropped connections, and the
HTTPS-only rules. A silent rule is indistinguishable from a clean site, and
two rules sat dead for months for exactly that reason, so they get real
fixtures here rather than a written excuse.
"""
from __future__ import annotations

import http.server
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path

from seo_scanner.config import ScannerConfig
from seo_scanner.runner import Scanner


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _self_signed(directory: Path) -> tuple[Path, Path]:
    """A throwaway certificate for a fixture server on localhost."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    # Fixed dates: the scanner never checks validity, and a frozen certificate
    # keeps the test deterministic.
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2040, 1, 1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return cert_path, key_path


def _cryptography_available() -> bool:
    try:
        import cryptography  # noqa: F401
    except Exception:
        return False
    return True


class HttpsHandler(http.server.BaseHTTPRequestHandler):
    """An HTTPS page that links and refers to insecure HTTP URLs."""

    http_origin = ""

    def do_GET(self):  # noqa: N802 - stdlib naming
        body = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>An HTTPS page referencing insecure URLs</title>"
            "<meta name=\"description\" content=\"A page that links to HTTP versions of its own URLs.\">"
            f"<link rel=\"stylesheet\" href=\"{self.http_origin}/style.css\">"
            "</head><body><h1>Insecure references</h1>"
            f"<p>{'Body copy for the insecure reference fixture. ' * 30}</p>"
            f"<a href=\"{self.http_origin}/other\">an insecure internal link</a>"
            f"<img src=\"{self.http_origin}/image.png\" alt=\"An insecure image\">"
            "</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


@unittest.skipUnless(_cryptography_available(), "cryptography is not installed")
class HttpsRuleTest(unittest.TestCase):
    def test_https_page_referencing_http_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = _self_signed(Path(tmp))
            port = _free_port()
            HttpsHandler.http_origin = f"http://127.0.0.1:{_free_port()}"
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), HttpsHandler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                config = ScannerConfig(max_pages=3, max_resources=5, timeout_seconds=5,
                                       verify_tls=False, render_enabled=False,
                                       accessibility_enabled=False)
                result = Scanner(config).scan(f"https://127.0.0.1:{port}/")
            finally:
                server.shutdown()
                server.server_close()
        fired = {issue.rule_id for issue in result.issues}
        self.assertIn("link.insecure_internal", fired)


class HangingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/":
            body = (
                "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<title>A page linking to a server that never answers</title></head>"
                "<body><h1>Hanging</h1><a href=\"/hangs\">hangs</a>"
                f"<p>{'Body copy for the hanging fixture. ' * 30}</p></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # Never respond: the scanner's read timeout must fire.
        threading.Event().wait(30)

    def log_message(self, *_):
        pass


class TimeoutRuleTest(unittest.TestCase):
    def test_a_page_that_never_answers_is_reported_as_a_timeout(self):
        port = _free_port()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), HangingHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            config = ScannerConfig(max_pages=5, max_resources=5, timeout_seconds=2,
                                   max_fetch_attempts=1, render_enabled=False,
                                   accessibility_enabled=False)
            result = Scanner(config).scan(f"http://127.0.0.1:{port}/")
        finally:
            server.shutdown()
            server.server_close()
        fired = {issue.rule_id for issue in result.issues}
        self.assertIn("page.timeout", fired)


class ResetHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/":
            body = (
                "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<title>A page linking to a connection that drops</title></head>"
                "<body><h1>Reset</h1><a href=\"/drops\">drops</a>"
                f"<p>{'Body copy for the reset fixture. ' * 30}</p></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.close_connection = True
        self.wfile.close()

    def log_message(self, *_):
        pass


class FetchFailureRuleTest(unittest.TestCase):
    def test_a_dropped_connection_is_reported_as_a_fetch_failure(self):
        port = _free_port()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), ResetHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            config = ScannerConfig(max_pages=5, max_resources=5, timeout_seconds=3,
                                   max_fetch_attempts=1, render_enabled=False,
                                   accessibility_enabled=False)
            result = Scanner(config).scan(f"http://127.0.0.1:{port}/")
        finally:
            server.shutdown()
            server.server_close()
        fired = {issue.rule_id for issue in result.issues}
        self.assertTrue(
            {"page.fetch_failed", "page.timeout"} & fired,
            msg=f"expected a fetch failure, got {sorted(fired)}",
        )


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_cryptography_available(), "cryptography is not installed")
class TlsErrorRuleTest(unittest.TestCase):
    def test_an_untrusted_certificate_is_reported(self):
        """With verification on — the default — a self-signed host is a TLS error."""
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = _self_signed(Path(tmp))
            port = _free_port()
            HttpsHandler.http_origin = f"http://127.0.0.1:{_free_port()}"
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), HttpsHandler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                config = ScannerConfig(max_pages=2, max_resources=2, timeout_seconds=5,
                                       max_fetch_attempts=1, render_enabled=False,
                                       accessibility_enabled=False)
                result = Scanner(config).scan(f"https://127.0.0.1:{port}/")
            finally:
                server.shutdown()
                server.server_close()
        self.assertIn("page.tls_error", {issue.rule_id for issue in result.issues})
