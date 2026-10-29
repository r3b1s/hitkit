"""The install seed disk for installer roles (docs/plans/incus-os.md §5.3)."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from hitkit.machine.seed import LABEL, create_seed_image

pytestmark = pytest.mark.skipif(not (shutil.which("mkfs.fat") and shutil.which("mtype")),
                                reason="needs dosfstools and mtools")


def _mtools(*argv: str) -> str:
    return subprocess.run(list(argv), capture_output=True, text=True, check=True).stdout


def test_seed_disk_is_a_labelled_fat_image_with_one_json_file_per_section(tmp_path):
    seed = tmp_path / "seed.img"
    sections = {
        "install": {"version": "1", "force_reboot": False, "target": {"id": "hitkit-data"}},
        "network": {"version": "1", "dns": {"hostname": "incus-os", "domain": "hitkit.internal"}},
        "migration-manager": {"version": "1"},  # long, hyphenated names survive (FAT long file names)
    }
    create_seed_image(seed, sections)

    assert seed.stat().st_size == 16 * 2**20
    assert LABEL in _mtools("mlabel", "-i", str(seed), "-s", "::")
    names = sorted(_mtools("mdir", "-b", "-i", str(seed), "::/").split())
    assert names == ["::/install.json", "::/migration-manager.json", "::/network.json"]
    for section, content in sections.items():
        assert json.loads(_mtools("mtype", "-i", str(seed), f"::/{section}.json")) == content


def test_rebuilding_replaces_the_previous_seed(tmp_path):
    seed = tmp_path / "seed.img"
    create_seed_image(seed, {"install": {"version": "1"}})
    create_seed_image(seed, {"network": {"version": "1"}})
    assert _mtools("mdir", "-b", "-i", str(seed), "::/").split() == ["::/network.json"]
