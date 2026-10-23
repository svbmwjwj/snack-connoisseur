"""A throwaway TLS server on 127.0.0.1 and certificate builders for the probe tests."""

from __future__ import annotations

import datetime
import socketserver
import ssl
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

HANDSHAKE_TIMEOUT = 2.0
POLL_INTERVAL = 0.02


class Certificate(NamedTuple):
    cert_pem: bytes
    key_pem: bytes

    @property
    def der(self) -> bytes:
        return ssl.PEM_cert_to_DER_cert(self.cert_pem.decode("ascii"))


def name(common_name: str | None = None, organization: str | None = None) -> x509.Name:
    attributes = []
    if organization is not None:
        attributes.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization))
    if common_name is not None:
        attributes.append(x509.NameAttribute(NameOID.COMMON_NAME, common_name))
    return x509.Name(attributes)


def make_certificate(
    subject: x509.Name,
    *,
    issuer: x509.Name | None = None,
    sans: Sequence[x509.GeneralName] = (),
) -> Certificate:
    """Build a certificate; it is self-signed (issuer == subject) unless ``issuer`` is given.

    The signature is always made with the certificate's own key: the probe never verifies it.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer if issuer is not None else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(list(sans)), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    return Certificate(
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def default_certificate() -> Certificate:
    """Self-signed certificate for probe.example.com, in both the SAN and the CN."""
    return make_certificate(name("probe.example.com"), sans=[x509.DNSName("probe.example.com")])


class _Handshake(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(HANDSHAKE_TIMEOUT)
        try:
            self.server.context.wrap_socket(self.request, server_side=True).close()
        except OSError:
            pass  # the probe hangs up as soon as it has read the certificate


class _Server(socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, context: ssl.SSLContext) -> None:
        super().__init__(("127.0.0.1", 0), _Handshake)
        self.context = context


class TlsServer:
    """Completes TLS handshakes on an ephemeral 127.0.0.1 port from a background thread.

    ``server_names`` lists the SNI of every handshake so far (``None`` for no SNI).
    """

    def __init__(
        self,
        certificate: Certificate,
        workdir: Path,
        *,
        alpn: Sequence[str] | None = ("h2",),
        tls_version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_3,
    ) -> None:
        cert_file, key_file = workdir / "server-cert.pem", workdir / "server-key.pem"
        cert_file.write_bytes(certificate.cert_pem)
        key_file.write_bytes(certificate.key_pem)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = tls_version
        context.load_cert_chain(cert_file, key_file)
        if alpn is not None:
            context.set_alpn_protocols(list(alpn))
        self.server_names: list[str | None] = []
        context.sni_callback = lambda _sock, server_name, _context: self.server_names.append(server_name)
        self._server = _Server(context)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(POLL_INTERVAL,), name="tls-test-server", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
