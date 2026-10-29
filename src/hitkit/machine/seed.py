"""The install seed disk for installer roles: a FAT filesystem labelled SEED_DATA holding
one ``<section>.json`` per seed section (docs/incus/incus-os-in-libvirt.md §2).

Built unprivileged with dosfstools and mtools, which `hitkit doctor` already requires.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from hitkit import trace
from hitkit.errors import HitkitError

LABEL = "SEED_DATA"
SIZE_KIB = 16384


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
