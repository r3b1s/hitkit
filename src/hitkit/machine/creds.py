"""Per-machine credentials, generated host-side and delivered via fw_cfg (design §8.5)."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hitkit.addressing import DOMAIN, address, slot
from hitkit.host.ca import CA, issue_leaf
from hitkit.paths import UserContext, ensure_private_dir, write_private

# QEMU limits fw_cfg file names to 55 characters, and every credential is named
# opt/io.systemd.credentials/<name> (27 characters of prefix).
MAX_NAME = 55 - len("opt/io.systemd.credentials/")


@dataclass(frozen=True)
class SSHKeyPair:
    private_openssh: bytes
    public_line: str  # "ssh-ed25519 AAAA..."


def ssh_keypair() -> SSHKeyPair:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode()
    return SSHKeyPair(private, public)


def client_key_path(user: UserContext) -> Path:
    return user.data_dir / "ssh" / "id_ed25519"


def ensure_client_key(user: UserContext) -> str:
    """The per-install key hitkit uses to SSH into machines. Returns its public line."""
    path = client_key_path(user)
    pub = path.with_suffix(".pub")
    if not path.exists():
        pair = ssh_keypair()
        ensure_private_dir(path.parent, user)
        write_private(path, pair.private_openssh, user, mode=0o600)
        write_private(pub, f"{pair.public_line} hitkit\n".encode(), user, mode=0o644)
    return pub.read_text().strip()


def network_unit(mac: str, ip: ipaddress.IPv4Address, subnet: ipaddress.IPv4Network) -> str:
    gateway = address(subnet, 1)
    return (
        "[Match]\n"
        f"MACAddress={mac}\n\n"
        "[Network]\n"
        f"Address={ip}/{subnet.prefixlen}\n"
        f"Gateway={gateway}\n"
        f"DNS={gateway}\n"
        f"Domains={DOMAIN}\n"
    )


# Credentials every machine gets; anything else comes from a kit's hooks.
COMMON = frozenset({
    "system.hostname", "hitkit.network", "hitkit.ssh-host-key", "hitkit.authorized-keys",
    "hitkit.tls.crt", "hitkit.tls.key", "hitkit.ca.crt",
})


def sync_kit_credentials(creds_dir: Path, extra: dict[str, bytes], user: UserContext) -> bool:
    """Make the kit-provided credentials on disk equal ``extra``. Returns True if anything changed."""
    clash = sorted(set(extra) & COMMON)
    if clash:  # validate before touching anything
        raise ValueError(f"kit credentials would override common credentials: {clash}")
    changed = False
    current = {p.name for p in creds_dir.iterdir()} - COMMON if creds_dir.exists() else set()
    for name in current - set(extra):
        (creds_dir / name).unlink()
        changed = True
    for name, content in extra.items():
        path = creds_dir / name
        if not path.exists() or path.read_bytes() != content:
            write_private(path, content, user, mode=0o600)
            changed = True
    return changed


@dataclass(frozen=True)
class MachineCredentials:
    files: dict[str, bytes]  # credential name -> content
    known_hosts: str


def build(
    machine: str,
    subnet: ipaddress.IPv4Network,
    mac: str,
    ca: CA,
    client_public_key: str,
    extra: dict[str, bytes] | None = None,
) -> MachineCredentials:
    ip = address(subnet, slot(machine).host_octet)
    fqdn = f"{machine}.{DOMAIN}"
    host_key = ssh_keypair()
    tls_key, tls_cert = issue_leaf(ca, [fqdn], [ip])
    files = {
        "system.hostname": machine.encode(),
        "hitkit.network": network_unit(mac, ip, subnet).encode(),
        "hitkit.ssh-host-key": host_key.private_openssh,
        "hitkit.authorized-keys": f"{client_public_key}\n".encode(),
        "hitkit.tls.crt": tls_cert,
        "hitkit.tls.key": tls_key,
        "hitkit.ca.crt": ca.cert_pem,
    }
    for name in extra or {}:
        if name in files:
            raise ValueError(f"kit credential {name!r} would override a common credential")
    files.update(extra or {})
    too_long = [n for n in files if len(n) > MAX_NAME]
    if too_long:
        raise ValueError(f"credential names longer than {MAX_NAME} characters: {too_long}")
    known_hosts = f"{fqdn},{machine},{ip} {host_key.public_line}\n"
    return MachineCredentials(files, known_hosts)


def write(creds_dir: Path, creds: MachineCredentials, user: UserContext) -> list[str]:
    ensure_private_dir(creds_dir, user)
    for name, content in creds.files.items():
        write_private(creds_dir / name, content, user, mode=0o600)
    return sorted(creds.files)
