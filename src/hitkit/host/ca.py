"""hitkit's local certificate authority (design §7.3).

No CA service: hitkit creates every machine, so it signs each leaf directly at
`up` time. The root is name-constrained to ``hitkit.internal`` and the hitkit
subnet, so even a stolen CA key cannot impersonate anything else — which is
what makes adding it to host and CI trust stores acceptable.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from hitkit.addressing import DOMAIN
from hitkit.paths import UserContext, ensure_private_dir, write_private

CA_VALIDITY = dt.timedelta(days=3650)
# Stay under the 398-day ceiling browsers apply to public CAs, so nothing ever
# has a reason to reject our leaves on lifetime alone.
LEAF_VALIDITY = dt.timedelta(days=397)

CA_KEY = "ca.key"
CA_CERT = "ca.crt"


@dataclass(frozen=True)
class CA:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate

    @property
    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    def permitted_subnets(self) -> list[ipaddress.IPv4Network]:
        nc = self.cert.extensions.get_extension_for_class(x509.NameConstraints).value
        return [g.value for g in nc.permitted_subtrees or [] if isinstance(g, x509.IPAddress)]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def create_ca(subnet: ipaddress.IPv4Network, install_id: str) -> CA:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "hitkit"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"hitkit local CA {install_id[:8]}"),
        ]
    )
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[x509.DNSName(DOMAIN), x509.IPAddress(subnet)],
                excluded_subtrees=None,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return CA(key, cert)


def issue_leaf(
    ca: CA,
    dns_names: list[str],
    ips: list[ipaddress.IPv4Address] = (),
    validity: dt.timedelta = LEAF_VALIDITY,
) -> tuple[bytes, bytes]:
    """Sign a TLS server certificate. Returns (key_pem, cert_pem)."""
    if not dns_names:
        raise ValueError("a leaf certificate needs at least one DNS name")
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])]))
        .issuer_name(ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + validity)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(n) for n in dns_names] + [x509.IPAddress(ip) for ip in ips]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.key.public_key()), critical=False
        )
        .sign(ca.key, hashes.SHA256())
    )
    return _key_pem(key), cert.public_bytes(serialization.Encoding.PEM)


def ca_cert_path(user: UserContext) -> Path:
    return user.ca_dir / CA_CERT


def load_ca(user: UserContext) -> CA | None:
    try:
        key_pem = (user.ca_dir / CA_KEY).read_bytes()
        cert_pem = (user.ca_dir / CA_CERT).read_bytes()
    except FileNotFoundError:
        return None
    key = serialization.load_pem_private_key(key_pem, password=None)
    return CA(key, x509.load_pem_x509_certificate(cert_pem))


def ensure_ca(user: UserContext, subnet: ipaddress.IPv4Network, install_id: str) -> CA:
    """Load the CA, creating it on first use. Refuses a CA constrained to another subnet."""
    ca = load_ca(user)
    if ca is not None:
        if subnet not in ca.permitted_subnets():
            raise ValueError(
                f"existing hitkit CA is constrained to {ca.permitted_subnets()}, not {subnet}"
            )
        return ca
    ca = create_ca(subnet, install_id)
    ensure_private_dir(user.ca_dir, user)
    write_private(user.ca_dir / CA_KEY, _key_pem(ca.key), user, mode=0o600)
    write_private(user.ca_dir / CA_CERT, ca.cert_pem, user, mode=0o644)
    return ca
