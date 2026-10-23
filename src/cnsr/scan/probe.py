"""Connect to neighbouring IPs without SNI and read the domain from each TLS 1.3 + h2 certificate."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass

from cryptography import x509
from cryptography.x509.oid import NameOID

TLS13 = "TLSv1.3"
_DNS_NAME = re.compile(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*")  # rules out wildcards, spaces and IPv6 literals


@dataclass(frozen=True)
class Hit:
    """A neighbour that answered with TLS 1.3 + h2 and a certificate naming a real domain."""

    ip: str
    domain: str
    issuer: str
    self_signed: bool


def scan(
    ips: list[str],
    *,
    progress: Callable[[int, int], None] | None = None,
    port: int = 443,
    concurrency: int = 64,
    timeout: float = 5.0,
) -> list[Hit]:
    """Probe every address (without SNI) and return the hits in the order of ``ips``.

    ``timeout`` bounds the connect plus the handshake of one address. An address that fails
    in any way is simply not a hit. ``progress(done, total)`` is called on this thread each
    time an address is finished; an exception it raises stops the scan and propagates.
    """
    return asyncio.run(_scan(ips, port, concurrency, timeout, progress))


def sni_ok(ip: str, domain: str, *, port: int = 443, timeout: float = 5.0) -> bool:
    """Whether a TLS 1.3 handshake with SNI ``domain`` completes; any failure is False."""
    try:
        version, _, _ = asyncio.run(_handshake_within(ip, port, domain, timeout))
    except Exception:
        return False
    return version == TLS13


async def _scan(
    ips: list[str], port: int, concurrency: int, timeout: float, progress: Callable[[int, int], None] | None
) -> list[Hit]:
    gate = asyncio.Semaphore(concurrency)
    done = 0

    async def probe_one(ip: str) -> Hit | None:
        nonlocal done
        async with gate:
            hit = await _probe(ip, port, timeout)
        done += 1
        if progress is not None:
            progress(done, len(ips))
        return hit

    results = await asyncio.gather(*(probe_one(ip) for ip in ips))
    return [hit for hit in results if hit is not None]


async def _probe(ip: str, port: int, timeout: float) -> Hit | None:
    try:
        # An empty server_hostname turns SNI off (and, with CERT_NONE, hostname matching).
        version, protocol, der = await _handshake_within(ip, port, "", timeout)
        if version != TLS13 or protocol != "h2" or not der:
            return None
        return _hit(ip, x509.load_der_x509_certificate(der))
    except Exception:  # one unreachable or odd neighbour must not stop the scan
        return None


async def _handshake_within(
    ip: str, port: int, server_hostname: str, timeout: float
) -> tuple[str | None, str | None, bytes | None]:
    return await asyncio.wait_for(_handshake(ip, port, server_hostname), timeout)


async def _handshake(ip: str, port: int, server_hostname: str) -> tuple[str | None, str | None, bytes | None]:
    """Complete a handshake and return (TLS version, ALPN protocol, DER certificate)."""
    _, writer = await asyncio.open_connection(ip, port, ssl=_context(), server_hostname=server_hostname)
    try:
        tls = writer.get_extra_info("ssl_object")
        return tls.version(), tls.selected_alpn_protocol(), tls.getpeercert(binary_form=True)
    finally:
        writer.transport.abort()  # skip the close_notify exchange: a silent peer would stall it


def _context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(["h2"])
    return context


def _hit(ip: str, cert: x509.Certificate) -> Hit | None:
    domain = _domain(cert)
    if domain is None:
        return None
    return Hit(ip, domain, _issuer(cert), cert.issuer == cert.subject)


def _domain(cert: x509.Certificate) -> str | None:
    """The first usable SAN DNS name, else the subject CN when that is a usable DNS name."""
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = san.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    for name in names:
        if (domain := _usable(name)) is not None:
            return domain
    common_names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return _usable(str(common_names[0].value)) if common_names else None


def _usable(name: str) -> str | None:
    """``name`` in lower case if it is a plain DNS name: no wildcard, not an IP address."""
    domain = name.lower()
    if _DNS_NAME.fullmatch(domain) is None or is_ip_address(domain):
        return None
    return domain


def _issuer(cert: x509.Certificate) -> str:
    common_names = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    if common_names and common_names[0].value:
        return str(common_names[0].value)
    return cert.issuer.rfc4514_string()


def is_ip_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True
