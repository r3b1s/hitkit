"""Per-machine credentials generated host-side (design §8.5)."""

from __future__ import annotations

import ipaddress
import stat

import pytest

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from hitkit.addressing import parse_subnet
from hitkit.host.ca import create_ca
from hitkit.machine import creds

SUBNET = parse_subnet("10.77.0.0/24")
MAC = "52:54:00:68:00:28"
IP = ipaddress.IPv4Address("10.77.0.40")
CLIENT_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIClient hitkit"


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_ssh_keypair_formats():
    pair = creds.ssh_keypair()
    assert pair.private_openssh.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
    assert pair.public_line.startswith("ssh-ed25519 ")


def test_ensure_client_key_creates_private_key_and_is_idempotent(user):
    first = creds.ensure_client_key(user)
    key_path = creds.client_key_path(user)
    assert first.startswith("ssh-ed25519 ")
    assert _mode(key_path) == 0o600
    assert key_path.read_bytes().startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")

    second = creds.ensure_client_key(user)
    assert second == first


def test_network_unit_contents():
    unit = creds.network_unit(MAC, IP, SUBNET)
    assert f"MACAddress={MAC}" in unit
    assert "Address=10.77.0.40/24" in unit
    assert "Gateway=10.77.0.1" in unit
    assert "DNS=10.77.0.1" in unit
    assert "Domains=hitkit.internal" in unit


def test_build_returns_expected_files_and_known_hosts():
    ca = create_ca(SUBNET, "install-abc")
    result = creds.build("devbox", SUBNET, MAC, ca, CLIENT_PUB)
    files = result.files

    assert set(files) == {
        "system.hostname",
        "hitkit.network",
        "hitkit.ssh-host-key",
        "hitkit.authorized-keys",
        "hitkit.tls.crt",
        "hitkit.tls.key",
        "hitkit.ca.crt",
    }
    assert files["system.hostname"] == b"devbox"
    assert files["hitkit.authorized-keys"].decode() == f"{CLIENT_PUB}\n"
    assert files["hitkit.ca.crt"] == ca.cert_pem

    assert result.known_hosts.startswith("devbox.hitkit.internal,devbox,10.77.0.40 ssh-ed25519 ")
    host_pub = result.known_hosts.split(" ", 1)[1].strip()
    assert host_pub.startswith("ssh-ed25519 ")


def test_build_leaf_certificate_covers_name_and_ip():
    ca = create_ca(SUBNET, "install-abc")
    files = creds.build("devbox", SUBNET, MAC, ca, CLIENT_PUB).files
    cert = x509.load_pem_x509_certificate(files["hitkit.tls.crt"])
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "devbox.hitkit.internal" in san.get_values_for_type(x509.DNSName)
    assert IP in san.get_values_for_type(x509.IPAddress)
    assert ExtendedKeyUsageOID.SERVER_AUTH in cert.extensions.get_extension_for_class(
        x509.ExtendedKeyUsage
    ).value


def test_build_uses_a_fresh_host_key_each_time():
    ca = create_ca(SUBNET, "install-abc")
    a = creds.build("devbox", SUBNET, MAC, ca, CLIENT_PUB)
    b = creds.build("devbox", SUBNET, MAC, ca, CLIENT_PUB)
    assert a.files["hitkit.ssh-host-key"] != b.files["hitkit.ssh-host-key"]
    assert a.known_hosts != b.known_hosts


def test_write_stores_every_file_private_and_returns_sorted_names(tmp_path, user):
    ca = create_ca(SUBNET, "install-abc")
    result = creds.build("devbox", SUBNET, MAC, ca, CLIENT_PUB)
    creds_dir = tmp_path / "vm" / "creds"

    names = creds.write(creds_dir, result, user)

    assert names == sorted(result.files)
    assert _mode(creds_dir) == 0o700
    for name, content in result.files.items():
        path = creds_dir / name
        assert path.read_bytes() == content
        assert _mode(path) == 0o600


def test_credential_names_fit_qemu_fw_cfg_limit(tmp_path):
    from hitkit.addressing import parse_subnet
    from hitkit.host.ca import create_ca
    from hitkit.machine import creds as creds_mod

    subnet = parse_subnet("10.77.0.0/24")
    ca = create_ca(subnet, "a" * 32)
    ok = creds_mod.build("devbox", subnet, "52:54:00:68:00:28", ca, "ssh-ed25519 AAAA test")
    assert all(len(n) <= creds_mod.MAX_NAME for n in ok.files)
    with pytest.raises(ValueError):
        creds_mod.build("devbox", subnet, "52:54:00:68:00:28", ca, "ssh-ed25519 AAAA test",
                        {"hitkit.forgejo.admin-password": b"x"})
