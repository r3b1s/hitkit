"""Host trust store integration (design §7.3). Requires root.

Arch and Fedora: p11-kit's `trust anchor` (it also feeds NSS/Firefox there).
Debian: /usr/local/share/ca-certificates + update-ca-certificates (Firefox
keeps its own store on Debian).
"""

from __future__ import annotations

from pathlib import Path

from cryptography import x509

from hitkit import trace


def _label(cert_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value


DEBIAN_DIR = Path("/usr/local/share/ca-certificates")


def _debian() -> bool:
    from hitkit import hostinfo

    return hostinfo.detect().family == "debian"


def _debian_path(cert_path: Path) -> Path:
    return DEBIAN_DIR / f"hitkit-{_label(cert_path).split()[-1]}.crt"


def is_installed(cert_path: Path) -> bool:
    if not cert_path.exists():
        return False
    if _debian():
        return _debian_path(cert_path).exists()
    out = trace.run(
        ["trust", "list", "--filter=ca-anchors"], capture_output=True, text=True, check=True
    ).stdout
    return f"label: {_label(cert_path)}" in out


def install(cert_path: Path) -> None:
    if _debian():
        DEBIAN_DIR.mkdir(parents=True, exist_ok=True)
        _debian_path(cert_path).write_bytes(cert_path.read_bytes())
        trace.run(["update-ca-certificates"], check=True, capture_output=True)
        return
    trace.run(["trust", "anchor", "--store", str(cert_path)], check=True)


def remove(cert_path: Path) -> None:
    if _debian():
        _debian_path(cert_path).unlink(missing_ok=True)
        trace.run(["update-ca-certificates", "--fresh"], check=True, capture_output=True)
        return
    trace.run(["trust", "anchor", "--remove", str(cert_path)], check=True)
