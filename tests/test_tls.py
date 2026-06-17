"""client_cert_from_pfx: round-trips a PKCS#12 (.pfx) into a combined PEM (key+cert+chain)."""

import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from crawlerkit.core.tls import client_cert_from_pfx


def _self_signed_pfx(tmp_path, password: bytes = b"testpass") -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "crawlerkit-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pfx_bytes = pkcs12.serialize_key_and_certificates(
        name=b"crawlerkit-test", key=key, cert=cert, cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(password),
    )
    pfx_path = tmp_path / "client.pfx"
    pfx_path.write_bytes(pfx_bytes)
    return str(pfx_path)


def test_client_cert_from_pfx_writes_combined_pem(tmp_path) -> None:
    pfx_path = _self_signed_pfx(tmp_path)
    out_path = str(tmp_path / "client.pem")

    pem_path = client_cert_from_pfx(pfx_path, "testpass", out_path=out_path)

    assert pem_path == out_path
    pem_contents = open(pem_path, encoding="utf-8").read()
    assert "-----BEGIN RSA PRIVATE KEY-----" in pem_contents  # TraditionalOpenSSL format
    assert "-----BEGIN CERTIFICATE-----" in pem_contents
