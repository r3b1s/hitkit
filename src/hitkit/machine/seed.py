"""The install seed disk for installer roles: a FAT filesystem labelled SEED_DATA holding
one ``<section>.json`` per seed section (docs/incus/incus-os-in-libvirt.md §2).

Built unprivileged with dosfstools and mtools, which `hitkit doctor` already requires.

An Incus VM takes one install medium more easily than two, so the seed can also go inside
the image: a tar at a fixed offset (docs/incus/operations-center.md §2).
"""

from __future__ import annotations

import gzip
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from hitkit import trace
from hitkit.errors import HitkitError

LABEL = "SEED_DATA"
SIZE_KIB = 16384
# The empty seed slot in IncusOS install images, the same in the raw and ISO forms. Operations
# Center's image flasher uses this constant too; a pin bump re-checks it (seeded_iso refuses a
# slot that is not empty).
IMAGE_SEED_OFFSET = 2148532224


def _run(argv: list[str]) -> None:
    try:
        trace.run(argv, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise HitkitError(f"{argv[0]} is not installed", hint="hitkit doctor lists the packages to install") from None
    except subprocess.CalledProcessError as e:
        raise HitkitError(f"{argv[0]} failed: {e.stderr.strip()}") from None


def create_seed_image(dest: Path, sections: dict[str, dict]) -> None:
    dest.unlink(missing_ok=True)
    _run(["mkfs.fat", "-C", "-n", LABEL, str(dest), str(SIZE_KIB)])
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for section, content in sections.items():
            f = Path(tmp) / f"{section}.json"
            f.write_text(json.dumps(content, indent=2))
            files.append(str(f))
        if files:
            _run(["mcopy", "-i", str(dest), *files, "::/"])


def seed_tar(sections: dict[str, dict]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for section, content in sections.items():
            data = json.dumps(content, indent=2).encode()
            info = tarfile.TarInfo(f"{section}.json")
            info.size, info.mode = len(data), 0o600
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def seeded_iso(src_gz: Path, dest: Path, sections: dict[str, dict]) -> None:
    """Unpack a gzipped install ISO to ``dest``, with the seed written into its seed slot."""
    blob = seed_tar(sections)
    with gzip.open(src_gz, "rb") as src, dest.open("w+b") as out:
        shutil.copyfileobj(src, out, 1 << 20)
        out.seek(IMAGE_SEED_OFFSET)
        if len(slot := out.read(len(blob))) < len(blob) or slot.strip(b"\0"):
            raise HitkitError(f"{src_gz.name} has no empty seed slot at {IMAGE_SEED_OFFSET}",
                              hint="the IncusOS image layout changed; see docs/incus/operations-center.md §2")
        out.seek(IMAGE_SEED_OFFSET)
        out.write(blob)
