"""qcow2 disks via qemu-img: OS overlays on cached images, and data disks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hitkit import trace
from hitkit.errors import HitkitError


def _qemu_img(*args: str) -> str:
    try:
        return trace.run(
            ["qemu-img", *args], capture_output=True, text=True, check=True
        ).stdout
    except FileNotFoundError:
        raise HitkitError("qemu-img not found", hint="pacman -S qemu-img") from None
    except subprocess.CalledProcessError as e:
        raise HitkitError(f"qemu-img {args[0]} failed: {e.stderr.strip()}") from None


def create_overlay(base: Path, overlay: Path) -> None:
    _qemu_img("create", "-q", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(overlay))


def create_data_disk(path: Path, size_gib: int) -> None:
    _qemu_img("create", "-q", "-f", "qcow2", str(path), f"{size_gib}G")


def convert_to_qcow2(raw: Path, dest: Path) -> None:
    _qemu_img("convert", "-q", "-f", "raw", "-O", "qcow2", str(raw), str(dest))


def compressed_copy(src: Path, dest: Path) -> None:
    """A standalone compressed qcow2 copy (backups); never shares a backing file with src."""
    _qemu_img("convert", "-q", "-c", "-O", "qcow2", str(src), str(dest))


def expand_copy(src: Path, dest: Path) -> None:
    """A standalone uncompressed qcow2 copy (restores: the result is written to again)."""
    _qemu_img("convert", "-q", "-O", "qcow2", str(src), str(dest))


def info(path: Path) -> dict:
    """qemu-img's view of a disk: virtual-size and actual-size in bytes (safe while in use)."""
    return json.loads(_qemu_img("info", "--output=json", "-U", str(path)))


def resize(path: Path, size_gib: int) -> None:
    """Grow an unused disk (callers refuse shrinking; qemu-img would truncate data)."""
    _qemu_img("resize", "-q", str(path), f"{size_gib}G")


def backing_chain(path: Path) -> list[Path]:
    """Every file in the disk's backing chain, excluding the disk itself."""
    info = json.loads(_qemu_img("info", "--backing-chain", "--output=json", "-U", str(path)))
    return [Path(layer["filename"]) for layer in info[1:]]
