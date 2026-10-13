"""Host distribution differences (tier-1: Arch/CachyOS; also Debian and Fedora).

Only the handful of things that genuinely differ live here: where QEMU's bridge
helper is, how the trust store is updated, and how to install packages.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Distro:
    family: str  # "arch" | "debian" | "fedora" | "unknown"
    pretty: str
    version_id: str = ""
    codename: str = ""
    id: str = ""  # os-release ID: "arch", or a derivative such as "cachyos"

    @property
    def tools_tree_release(self) -> str | None:
        """Build mkosi's tools tree for the host's own release: its signing keys
        are already installed (mkosi's default for Fedora is rawhide)."""
        return {"fedora": self.version_id, "debian": self.codename}.get(self.family) or None

    @property
    def bridge_helper_candidates(self) -> tuple[Path, ...]:
        return (Path("/usr/lib/qemu/qemu-bridge-helper"), Path("/usr/libexec/qemu-bridge-helper"))

    @property
    def host_packages(self) -> str:
        return {
            "arch": "pacman -S --needed libvirt qemu-base edk2-ovmf dnsmasq mkosi systemd-ukify "
                    "apt debian-archive-keyring dosfstools mtools e2fsprogs",
            "debian": "apt install libvirt-daemon-system qemu-system-x86 qemu-utils ovmf dnsmasq-base "
                      "mkosi systemd-ukify python3-libvirt dosfstools mtools e2fsprogs",
            "fedora": "dnf install libvirt-daemon-kvm libvirt-daemon-config-network qemu-kvm edk2-ovmf "
                      "mkosi systemd-ukify distribution-gpg-keys apt debian-keyring python3-libvirt dosfstools mtools e2fsprogs",
        }.get(self.family, "install libvirt, QEMU/KVM, OVMF, mkosi and systemd-ukify")


def parse_os_release(text: str) -> Distro:
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip().strip('"')
    ids = [fields.get("ID", "")] + fields.get("ID_LIKE", "").split()
    family = "unknown"
    for candidate in ids:
        if candidate in ("arch", "debian", "fedora"):
            family = candidate
            break
        if candidate in ("ubuntu", "cachyos", "manjaro", "endeavouros", "rhel", "centos"):
            family = {"ubuntu": "debian", "rhel": "fedora", "centos": "fedora"}.get(candidate, "arch")
            break
    return Distro(family, fields.get("PRETTY_NAME", family),
                  fields.get("VERSION_ID", ""), fields.get("VERSION_CODENAME", ""), fields.get("ID", ""))


def detect(os_release: Path = Path("/etc/os-release")) -> Distro:
    try:
        return parse_os_release(os_release.read_text())
    except FileNotFoundError:
        return Distro("unknown", "unknown")


def bridge_helper(distro: Distro | None = None) -> Path | None:
    for path in (distro or detect()).bridge_helper_candidates:
        if path.exists():
            return path
    return None


def ovmf_available() -> bool:
    """QEMU firmware descriptors are what libvirt's firmware autoselection uses."""
    dirs = [Path("/usr/share/qemu/firmware"), Path("/etc/qemu/firmware")]
    return any(d.is_dir() and any(d.glob("*.json")) for d in dirs)


# Package manager + keyring a host needs to build each image distribution
# natively; without them hitkit uses mkosi's tools tree instead.
NATIVE_TOOLS = {
    "debian": (("apt",), ("/usr/share/keyrings/debian-archive-keyring.gpg",)),
    "arch": (("pacman",), ("/usr/share/pacman/keyrings/archlinux.gpg",)),
    "fedora": (("dnf",), ()),
}
COMMON_TOOLS = ("ukify", "systemd-repart", "mkfs.vfat", "mkfs.ext4")


# Non-root PATHs (e.g. Debian's) omit the sbin directories where mkfs.* live.
_SEARCH_PATH = ":".join(["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"])


def _have(binary: str) -> bool:
    return shutil.which(binary, path=_SEARCH_PATH) is not None or (
        binary == "ukify" and Path("/usr/lib/systemd/ukify").exists())


def can_build_natively(image_distribution: str, host: Distro | None = None) -> bool:
    # Arch derivatives (CachyOS, Manjaro, EndeavourOS) ship their own pacman builds, which
    # can write a local package database newer than Arch's pacman in the image reads.
    if image_distribution == "arch" and (host or detect()).id != "arch":
        return False
    bins, files = NATIVE_TOOLS.get(image_distribution, ((), ()))
    return all(_have(b) for b in (*bins, *COMMON_TOOLS)) and all(Path(f).exists() for f in files)


MKOSI_UPGRADE_HINT = "pip install 'git+https://github.com/systemd/mkosi@v27.1' into hitkit's venv (hitkit prefers it)"


def mkosi_binary() -> str | None:
    """mkosi from hitkit's own environment if installed there, else from PATH."""
    import sys

    local = Path(sys.executable).parent / "mkosi"
    return str(local) if local.exists() else shutil.which("mkosi", path=_SEARCH_PATH)


def mkosi_version(binary: str | None = None) -> tuple[int, ...] | None:
    import re

    from hitkit import trace

    binary = binary or mkosi_binary()
    if binary is None:
        return None
    out = trace.run([binary, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)(?:\.(\d+))?", out)
    return tuple(int(g) for g in m.groups() if g is not None) if m else None


def mkosi_problem(version: tuple[int, ...] | None) -> str | None:
    """Known-bad mkosi versions for hitkit's images, or None if fine."""
    if version is None:
        return "mkosi not found"
    if version[0] < 25:
        return f"mkosi {'.'.join(map(str, version))} is too old (need 25 or newer)"
    if version[0] == 26:
        return "mkosi 26 cannot build Debian images (it runs depmod inside the image chroot)"
    return None
