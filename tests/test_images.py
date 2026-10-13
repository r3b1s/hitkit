"""Cached machine images: config hashing, role resolution, manifests, cache listing (design §8.1)."""

from __future__ import annotations

import json
import os

import pytest

from hitkit.errors import HitkitError
from hitkit.machine import images
from hitkit.machine.images import Image


@pytest.fixture
def role_tree(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "mkosi.conf").write_text("[Distribution]\nDistribution=debian\n")
    (base / "mkosi.extra").mkdir()
    script = base / "mkosi.extra" / "provision"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o644)
    return [base]


def test_config_hash_is_stable_across_calls(role_tree):
    assert images.config_hash(role_tree) == images.config_hash(role_tree)


def test_config_hash_changes_when_content_changes(role_tree):
    before = images.config_hash(role_tree)
    (role_tree[0] / "mkosi.conf").write_text("[Distribution]\nDistribution=debian\nRelease=trixie\n")
    assert images.config_hash(role_tree) != before


def test_config_hash_changes_when_exec_bit_changes(role_tree):
    before = images.config_hash(role_tree)
    script = role_tree[0] / "mkosi.extra" / "provision"
    os.chmod(script, 0o755)
    assert images.config_hash(role_tree) != before


def test_config_hash_is_twelve_hex_chars(role_tree):
    digest = images.config_hash(role_tree)
    assert len(digest) == 12
    int(digest, 16)


def test_role_dirs_are_common_then_role():
    assert images.role_dirs("base") == [images.IMAGES_DIR / "common", images.IMAGES_DIR / "base"]
    assert images.role_dirs("hitkit-dev")[-1] == images.IMAGES_DIR / "hitkit-dev"


def test_common_is_not_a_role():
    with pytest.raises(HitkitError):
        images.role_dirs("common")


def test_role_dirs_unknown_role_raises():
    with pytest.raises(HitkitError, match="unknown image role"):
        images.role_dirs("nope")


def test_versions_from_manifest_keeps_only_tracked_packages():
    manifest = {
        "config": {"distribution": "debian", "release": "trixie"},
        "packages": [
            {"name": "systemd", "version": "257"},
            {"name": "zsh", "version": "1"},
        ],
    }
    assert images.versions_from_manifest(manifest) == {
        "distribution": "debian trixie",
        "systemd": "257",
    }


def test_list_images_empty_without_cache(user):
    assert images.list_images(user) == []


def _place_image(user, role: str, digest: str) -> Image:
    d = images.images_dir(user)
    d.mkdir(parents=True, exist_ok=True)
    image_id = f"{role}-{digest}"
    (d / f"{image_id}.qcow2").write_bytes(b"QFI\xfb")
    (d / f"{image_id}.json").write_text(
        json.dumps({
            "role": role,
            "hash": digest,
            "built": "2026-10-12T00:00:00+00:00",
            "versions": {"distribution": "debian trixie", "systemd": "257"},
        })
    )
    (d / f"{image_id}.manifest.json").write_text("{}")
    return Image(role, digest, d / f"{image_id}.qcow2", {})


def test_list_images_returns_summaries_and_skips_manifests(user):
    _place_image(user, "base", "0123456789ab")

    listed = images.list_images(user)

    assert len(listed) == 1
    image = listed[0]
    assert image.id == "base-0123456789ab"
    assert image.role == "base"
    assert image.hash == "0123456789ab"
    assert image.path == images.images_dir(user) / "base-0123456789ab.qcow2"
    assert image.versions == {"distribution": "debian trixie", "systemd": "257"}


def test_remove_deletes_qcow2_summary_and_manifest(user):
    _place_image(user, "base", "0123456789ab")
    _place_image(user, "devbox", "fedcba987654")
    base = next(i for i in images.list_images(user) if i.role == "base")

    images.remove(base)

    d = images.images_dir(user)
    assert not (d / "base-0123456789ab.qcow2").exists()
    assert not (d / "base-0123456789ab.json").exists()
    assert not (d / "base-0123456789ab.manifest.json").exists()
    # Unrelated images are untouched.
    assert (d / "devbox-fedcba987654.qcow2").exists()
    assert (d / "devbox-fedcba987654.json").exists()


def test_force_rmtree_removes_read_only_trees(tmp_path):
    root = tmp_path / "w"
    inner = root / "mkosi.tools" / "usr"
    inner.mkdir(parents=True)
    (inner / "f").write_text("x")
    inner.chmod(0o555)
    (root / "mkosi.tools").chmod(0o555)
    images._force_rmtree(root)
    assert not root.exists()


def test_image_builds_wait_for_each_other(user, monkeypatch):
    # Regression: a concurrent build (the pool rebuilding its runner image) wiped the shared
    # working tree under another build.
    import fcntl
    import threading

    from hitkit.machine import images

    lock_path = user.cache_dir / "build" / ".lock"
    lock_path.parent.mkdir(parents=True)
    held = lock_path.open("w")
    fcntl.flock(held, fcntl.LOCK_EX)
    messages, built = [], []
    monkeypatch.setattr(images, "_build", lambda u, role, log: built.append(role) or images.Image(role, "h", lock_path, {}))
    threading.Timer(0.2, lambda: (fcntl.flock(held, fcntl.LOCK_UN), held.close())).start()

    images.build(user, "base", messages.append)

    assert built == ["base"]
    assert any("another image build is running" in m for m in messages)


def test_extra_packages_join_the_image_id_only_when_present(user):
    from hitkit.machine import images

    plain = images.image_id("runner-arch", user)
    assert plain == images.image_id("runner-arch")  # no list: ids never move

    path = images.user_config_path(user, "runner-arch")
    path.parent.mkdir(parents=True)
    path.write_text('packages = ["rust", "go", "rust"]\n')

    assert images.extra_packages(user, "runner-arch") == ["go", "rust"]
    assert images.image_id("runner-arch", user) != plain  # changing the list makes machines outdated


def test_bad_package_lists_are_rejected(user):
    import pytest

    from hitkit.errors import HitkitError
    from hitkit.machine import images

    path = images.user_config_path(user, "runner-arch")
    path.parent.mkdir(parents=True)
    path.write_text('packages = ["ok", "evil; rm -rf /"]\n')

    with pytest.raises(HitkitError, match="list of package names"):
        images.extra_packages(user, "runner-arch")


def test_runner_arch_role_is_arch_with_the_forgejo_runner_pin():
    from hitkit.machine import images

    assert images.distribution("runner-arch") == "arch"
    pins = {a.name: a for a in images.pinned_artifacts(images.role_dirs("runner-arch"))}
    runner = {a.name: a for a in images.pinned_artifacts(images.role_dirs("runner"))}
    assert pins["forgejo-runner"] == runner["forgejo-runner"]  # bumped together


def test_arch_roles_share_the_arch_common_layer():
    from hitkit.machine import images

    for role in ("runner-arch", "hitkit-dev"):
        assert images.IMAGES_DIR / "arch-common" in images.role_dirs(role)
