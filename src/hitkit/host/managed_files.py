"""Edits to system files owned by other packages: /etc/hosts and /etc/qemu/bridge.conf.

All transforms are pure (text in, text out) so they are trivially testable and
idempotent; `write_system_file` is the only side effect.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path

HOSTS_PATH = Path("/etc/hosts")
BRIDGE_CONF_PATH = Path("/etc/qemu/bridge.conf")

BEGIN = "# BEGIN hitkit — managed by `hitkit setup`; do not edit"
END = "# END hitkit"


def remove_hosts_block(text: str) -> str:
    out, inside = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped == BEGIN:
            inside = True
            continue
        if inside and stripped == END:
            inside = False
            continue
        if not inside:
            out.append(line)
    if inside:
        raise ValueError("hitkit block in hosts file has no END marker; refusing to edit")
    return "".join(out)


def apply_hosts_block(text: str, records: list[tuple[ipaddress.IPv4Address, str]]) -> str:
    base = remove_hosts_block(text)
    if base and not base.endswith("\n"):
        base += "\n"
    lines = [BEGIN] + [f"{ip}\t{fqdn}" for ip, fqdn in records] + [END]
    return base + "\n".join(lines) + "\n"


def has_bridge_allow(text: str, bridge: str) -> bool:
    return any(line.split() == ["allow", bridge] for line in text.splitlines())


def ensure_bridge_allow(text: str, bridge: str) -> str:
    if has_bridge_allow(text, bridge):
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"allow {bridge}\n"


def remove_bridge_allow(text: str, bridge: str) -> str:
    return "".join(
        line for line in text.splitlines(keepends=True) if line.split() != ["allow", bridge]
    )


def read_or_empty(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def write_system_file(path: Path, text: str, mode: int = 0o644) -> None:
    """Atomically replace a root-owned system file, keeping its existing mode."""
    try:
        mode = path.stat().st_mode & 0o7777
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.hitkit-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
