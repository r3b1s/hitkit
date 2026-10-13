import datetime as dt
import ipaddress
import stat

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

from hitkit.addressing import parse_subnet
from hitkit.host.ca import create_ca, ensure_ca, issue_leaf

SUBNET = parse_subnet("10.77.0.0/24")
INSTALL_ID = "abcdef1234"


@pytest.fixture
def ca():
    return create_ca(SUBNET, INSTALL_ID)


@pytest.fixture
def leaf(ca):
    _, cert_pem = issue_leaf(
        ca, ["forgejo.hitkit.internal"], [ipaddress.IPv4Address("10.77.0.10")]
    )
    return x509.load_pem_x509_certificate(cert_pem)


def _verify(ca, cert, name):
    verifier = (
        PolicyBuilder()
        .store(Store([ca.cert]))
        .time(dt.datetime.now(dt.timezone.utc))
        .build_server_verifier(x509.DNSName(name))
    )
    return verifier.verify(cert, [])


def test_root_is_ca_with_path_length_zero(ca):
    ext = ca.cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert ext.value.ca is True
    assert ext.value.path_length == 0


def test_name_constraints_are_critical_and_exact(ca):
    ext = ca.cert.extensions.get_extension_for_class(x509.NameConstraints)
    assert ext.critical is True
    assert ext.value.permitted_subtrees == [x509.DNSName("hitkit.internal"), x509.IPAddress(SUBNET)]


def test_permitted_subnets(ca):
    assert ca.permitted_subnets() == [SUBNET]


def test_leaf_san_has_dns_name_and_ip(leaf):
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["forgejo.hitkit.internal"]
    assert san.get_values_for_type(x509.IPAddress) == [ipaddress.IPv4Address("10.77.0.10")]


def test_leaf_is_server_auth_and_not_ca(leaf):
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    bc = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is False


def test_leaf_validity_at_most_398_days(leaf):
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc <= dt.timedelta(days=398)


def test_issue_leaf_returns_pem_key_and_cert(ca):
    key_pem, cert_pem = issue_leaf(ca, ["forgejo.hitkit.internal"])
    assert key_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")


def test_leaf_chains_to_ca_for_its_name(ca, leaf):
    # Raises VerificationError on failure; returning means the chain is valid.
    _verify(ca, leaf, "forgejo.hitkit.internal")


def test_leaf_for_foreign_name_fails_name_constraints(ca):
    _, cert_pem = issue_leaf(ca, ["evil.example.com"])
    evil = x509.load_pem_x509_certificate(cert_pem)
    with pytest.raises(VerificationError):
        _verify(ca, evil, "evil.example.com")


def test_issue_leaf_requires_a_dns_name(ca):
    with pytest.raises(ValueError):
        issue_leaf(ca, [])


def test_ensure_ca_writes_key_0600_and_cert(user):
    ensure_ca(user, SUBNET, INSTALL_ID)
    key_path = user.ca_dir / "ca.key"
    cert_path = user.ca_dir / "ca.crt"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert x509.load_pem_x509_certificate(cert_path.read_bytes()).subject is not None


def test_ensure_ca_second_call_loads_same_cert(user):
    first = ensure_ca(user, SUBNET, INSTALL_ID)
    cert_bytes = (user.ca_dir / "ca.crt").read_bytes()
    second = ensure_ca(user, SUBNET, INSTALL_ID)
    assert second.cert == first.cert
    assert (user.ca_dir / "ca.crt").read_bytes() == cert_bytes


def test_ensure_ca_refuses_different_subnet(user):
    ensure_ca(user, SUBNET, INSTALL_ID)
    with pytest.raises(ValueError, match="constrained"):
        ensure_ca(user, parse_subnet("10.88.0.0/24"), INSTALL_ID)
