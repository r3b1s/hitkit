"""Pinned third-party artifacts: role resolution, download cache, staging, config hashing (design §8.1)."""

from __future__ import annotations

import hashlib
import io
import re
import stat

import pytest

from hitkit.errors import HitkitError
from hitkit.machine import images
from hitkit.machine.images import Artifact

PAYLOAD = b"#!/bin/sh\necho forgejo\n"
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()
NOOP = lambda _msg: None  # noqa: E731

PINS = """[[artifact]]
name = "forgejo"
version = "{version}"
url = "https://example.invalid/forgejo"
sha256 = "{sha}"
dest = "/usr/local/bin/forgejo"
mode = 0o755
"""


def _artifact(sha: str = PAYLOAD_SHA) -> Artifact:
    return Artifact(
        name="forgejo", version="16.0.5", url="https://example.invalid/forgejo",
        sha256=sha, dest="/usr/local/bin/forgejo", mode=0o755,
    )


@pytest.fixture
def downloads(monkeypatch):
    """Serve PAYLOAD for every URL; returns the list of URLs requested."""
    requested = []

    def fake_urlopen(url, timeout=None):
        requested.append(url)
        return io.BytesIO(PAYLOAD)

    monkeypatch.setattr(images.urllib.request, "urlopen", fake_urlopen)
    return requested


# --- role resolution and pins ----------------------------------------------


def test_forgejo_role_dirs_are_common_base_then_role():
    assert images.role_dirs("forgejo") == [
        images.IMAGES_DIR / "common", images.IMAGES_DIR / "base", images.IMAGES_DIR / "forgejo",
    ]


def test_runner_role_dirs_end_with_base_then_runner():
    assert images.role_dirs("runner")[-2:] == [images.IMAGES_DIR / "base", images.IMAGES_DIR / "runner"]


def test_forgejo_pins_exactly_one_artifact():
    (art,) = images.pinned_artifacts(images.role_dirs("forgejo"))
    assert art.name == "forgejo"
    assert re.fullmatch(r"[0-9a-f]{64}", art.sha256)
    assert art.dest == "/usr/local/bin/forgejo"
    assert art.mode == 0o755


def test_base_role_has_no_artifacts():
    assert images.pinned_artifacts(images.role_dirs("base")) == []


def test_changing_pins_toml_changes_config_hash(tmp_path):
    role = tmp_path / "forgejo"
    role.mkdir()
    (role / "mkosi.conf").write_text("[Distribution]\nDistribution=debian\n")
    (role / "pins.toml").write_text(PINS.format(version="16.0.5", sha=PAYLOAD_SHA))
    before = images.config_hash([role])

    (role / "pins.toml").write_text(PINS.format(version="16.0.6", sha=PAYLOAD_SHA))

    assert images.config_hash([role]) != before


def test_pins_toml_parses_into_artifacts(tmp_path):
    role = tmp_path / "forgejo"
    role.mkdir()
    (role / "pins.toml").write_text(PINS.format(version="16.0.5", sha=PAYLOAD_SHA))

    (art,) = images.pinned_artifacts([role])

    assert art.version == "16.0.5"
    assert art.mode == 0o755


# --- fetch -----------------------------------------------------------------


def test_fetch_verifies_and_caches_under_downloads(user, downloads):
    path = images.fetch_artifact(user, _artifact(), NOOP)

    assert path == user.cache_dir / "downloads" / PAYLOAD_SHA
    assert path.read_bytes() == PAYLOAD


def test_second_fetch_is_served_from_cache(user, downloads):
    first = images.fetch_artifact(user, _artifact(), NOOP)
    second = images.fetch_artifact(user, _artifact(), NOOP)

    assert first == second
    assert len(downloads) == 1


def test_checksum_mismatch_raises_and_leaves_no_file(user, downloads):
    with pytest.raises(HitkitError, match="checksum mismatch"):
        images.fetch_artifact(user, _artifact(sha="0" * 64), NOOP)

    assert list((user.cache_dir / "downloads").iterdir()) == []


# --- staging ---------------------------------------------------------------


def test_stage_places_artifact_at_dest_with_mode(user, downloads, tmp_path):
    staging = tmp_path / "staging"

    images.stage_artifacts(user, [_artifact()], staging, NOOP)

    target = staging / "usr" / "local" / "bin" / "forgejo"
    assert target.read_bytes() == PAYLOAD
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_fetch_artifact_cleans_up_partial_download(user, monkeypatch):
    from hitkit.errors import HitkitError

    class Broken:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            raise OSError("connection reset")

    monkeypatch.setattr(images.urllib.request, "urlopen", lambda *a, **k: Broken())
    art = images.Artifact("x", "1", "https://example.invalid/x", "0" * 64, "/usr/local/bin/x", 0o755)
    with pytest.raises(HitkitError):
        images.fetch_artifact(user, art, lambda m: None)
    assert not list((user.cache_dir / "downloads").glob("*"))


# --- installer roles (plan §4.1, §9) ---------------------------------------


def test_incus_os_role_dirs_is_just_incus_os():
    assert images.role_dirs("incus-os") == [images.IMAGES_DIR / "incus-os"]


def test_incus_os_pins_parse_as_installer_artifact():
    arts = images.pinned_artifacts(images.role_dirs("incus-os"))
    assert len(arts) == 1
    art = arts[0]
    assert art.name == "IncusOS"
    assert art.version == "202610272235"
    assert re.fullmatch(r"[0-9a-f]{64}", art.sha256)
    assert "IncusOS_202610272235.img.gz" in art.url


def test_changing_installer_toml_changes_config_hash(tmp_path):
    role = tmp_path / "incus-os"
    role.mkdir()
    (role / "installer.toml").write_text('[installer]\nname = "IncusOS"\nversion = "1"\nurl = "https://x"\nsha256 = "aaa"\n')
    before = images.config_hash([role])

    (role / "installer.toml").write_text('[installer]\nname = "IncusOS"\nversion = "2"\nurl = "https://x"\nsha256 = "bbb"\n')
    assert images.config_hash([role]) != before

