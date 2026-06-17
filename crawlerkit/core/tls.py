"""Per-host CA bundle builder with AIA repair.

Some hosts (e.g. Detran) serve only their leaf certificate and omit the intermediate, so
Python TLS verification fails with "unable to get local issuer certificate". A browser papers
over this by fetching the missing intermediate from the leaf's AIA "CA Issuers" URL; we do the
same here, generically: read the leaf's AIA, fetch + follow intermediates up to a trusted root,
and concatenate with certifi's roots. Verification stays ON. Cached per host.
"""

import os
import socket
import ssl
import tempfile
import urllib.request

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

_CACHE_DIR = os.environ.get(
    "CRAWLERKIT_CA_DIR", os.path.join(tempfile.gettempdir(), "crawlerkit-ca")
)


def _leaf_cert(host: str, port: int = 443) -> x509.Certificate:
    ctx = ssl._create_unverified_context()
    with socket.create_connection((host, port), timeout=30) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der = ssock.getpeercert(binary_form=True)
    return x509.load_der_x509_certificate(der)


def _ca_issuer_urls(cert: x509.Certificate) -> list[str]:
    try:
        aia = cert.extensions.get_extension_for_oid(
            ExtensionOID.AUTHORITY_INFORMATION_ACCESS
        ).value
    except x509.ExtensionNotFound:
        return []
    return [
        d.access_location.value
        for d in aia  # type: ignore[attr-defined]  # AuthorityInformationAccess IS iterable at runtime; get_extension_for_oid()'s generic return type just can't be narrowed statically
        if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS
    ]


def _fetch_cert(url: str) -> x509.Certificate:
    with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 (public CA cert, http ok)
        raw = r.read()
    if raw.lstrip().startswith(b"-----BEGIN"):
        return x509.load_pem_x509_certificate(raw)
    return x509.load_der_x509_certificate(raw)


def build_ca_bundle(host: str, port: int = 443, *, force: bool = False, max_depth: int = 4) -> str:
    """Return a path to a CA bundle = trusted roots + any intermediates `host` omits.

    Best-effort: if AIA repair fails, falls back to certifi roots only.
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)
    path = os.path.join(_CACHE_DIR, f"{host}_{port}.pem")
    if os.path.exists(path) and not force:
        return path

    roots = open(certifi.where(), encoding="utf-8").read()
    extra: list[str] = []
    try:
        cert = _leaf_cert(host, port)
        seen: set[str] = set()
        for _ in range(max_depth):
            urls = [u for u in _ca_issuer_urls(cert) if u.startswith(("http://", "https://"))]
            if not urls or urls[0] in seen:
                break
            seen.add(urls[0])
            cert = _fetch_cert(urls[0])
            extra.append(cert.public_bytes(serialization.Encoding.PEM).decode())
            if cert.issuer == cert.subject:  # reached a self-signed root
                break
    except Exception:  # noqa: BLE001 — never let CA discovery crash a crawl; roots-only fallback
        pass

    with open(path, "w", encoding="utf-8") as f:
        f.write(roots if roots.endswith("\n") else roots + "\n")
        for pem in extra:
            f.write(pem if pem.endswith("\n") else pem + "\n")
    return path


def client_cert_from_pfx(pfx_path: str, password: str | bytes | None, out_path: str | None = None) -> str:
    """Load an ICP-Brasil / PKCS#12 `.pfx` and write a combined PEM (private key + cert + CA chain)
    for curl_cffi's `cert=` (mutual TLS). Returns the PEM path. Port of alexandria/pfx_to_pem via
    `cryptography` (no pyOpenSSL). The output is chmod 600 (contains the private key)."""
    if isinstance(password, str):
        password = password.encode()
    with open(pfx_path, "rb") as f:
        data = f.read()
    key, cert, extra = pkcs12.load_key_and_certificates(data, password)
    os.makedirs(_CACHE_DIR, exist_ok=True)
    out_path = out_path or os.path.join(_CACHE_DIR, os.path.basename(pfx_path) + ".pem")
    with open(out_path, "wb") as f:
        if key is not None:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        if cert is not None:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        for ca in (extra or []):
            f.write(ca.public_bytes(serialization.Encoding.PEM))
    os.chmod(out_path, 0o600)
    return out_path
