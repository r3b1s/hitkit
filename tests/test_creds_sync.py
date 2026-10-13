"""Kit credential sync: the files on disk follow the kit's credentials; common ones are never touched."""

from __future__ import annotations

import stat

import pytest

from hitkit.machine import creds


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_new_credentials_are_written_private_and_report_a_change(user, tmp_path):
    directory = tmp_path / "creds"

    assert creds.sync_kit_credentials(directory, {"hitkit.runner.token": b"t"}, user) is True

    path = directory / "hitkit.runner.token"
    assert path.read_bytes() == b"t"
    assert _mode(path) == 0o600


def test_same_content_again_reports_no_change(user, tmp_path):
    directory = tmp_path / "creds"
    creds.sync_kit_credentials(directory, {"a": b"1"}, user)

    assert creds.sync_kit_credentials(directory, {"a": b"1"}, user) is False


def test_changed_content_is_rewritten(user, tmp_path):
    directory = tmp_path / "creds"
    creds.sync_kit_credentials(directory, {"a": b"1"}, user)

    assert creds.sync_kit_credentials(directory, {"a": b"2"}, user) is True
    assert (directory / "a").read_bytes() == b"2"


def test_removed_kit_credential_is_deleted(user, tmp_path):
    directory = tmp_path / "creds"
    creds.sync_kit_credentials(directory, {"a": b"1", "b": b"2"}, user)

    assert creds.sync_kit_credentials(directory, {"a": b"1"}, user) is True

    assert (directory / "a").exists()
    assert not (directory / "b").exists()


def test_common_credentials_are_never_deleted(user, tmp_path):
    directory = tmp_path / "creds"
    directory.mkdir()
    for name in creds.COMMON:
        (directory / name).write_bytes(b"x")

    assert creds.sync_kit_credentials(directory, {}, user) is False
    assert all((directory / name).exists() for name in creds.COMMON)


def test_kit_credential_named_like_a_common_one_is_rejected(user, tmp_path):
    with pytest.raises(ValueError, match="common credential"):
        creds.sync_kit_credentials(tmp_path / "creds", {"hitkit.network": b"x"}, user)


def test_rejected_common_name_leaves_existing_kit_credentials_alone(user, tmp_path):
    directory = tmp_path / "creds"
    creds.sync_kit_credentials(directory, {"stale": b"1"}, user)

    with pytest.raises(ValueError):
        creds.sync_kit_credentials(directory, {"hitkit.network": b"x"}, user)

    assert (directory / "stale").exists()
