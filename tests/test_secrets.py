"""Host-side secret store and generators (hitkit.secrets)."""

from __future__ import annotations

import base64
import re
import stat

from hitkit import secrets

KIT = "forge"


def test_get_returns_none_when_absent(user):
    assert secrets.get(user, KIT, "missing") is None


def test_put_then_get_round_trips(user):
    secrets.put(user, KIT, "token", "abc")
    assert secrets.get(user, KIT, "token") == "abc"


def test_put_writes_mode_0600_under_kit_dir(user):
    secrets.put(user, KIT, "token", "abc")
    path = secrets.kit_dir(user, KIT) / "token"
    assert path.parent == user.data_dir / "secrets" / KIT
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_get_or_create_generates_once_then_reuses(user):
    calls = []

    def generate():
        calls.append(1)
        return "generated"

    assert secrets.get_or_create(user, KIT, "s", generate) == "generated"
    assert secrets.get_or_create(user, KIT, "s", generate) == "generated"
    assert len(calls) == 1


def test_remove_kit_deletes_its_directory_only(user):
    secrets.put(user, KIT, "a", "1")
    secrets.put(user, "other", "b", "2")

    secrets.remove_kit(user, KIT)

    assert not secrets.kit_dir(user, KIT).exists()
    assert secrets.get(user, "other", "b") == "2"


def test_hex_secret_has_twice_nbytes_hex_chars():
    assert re.fullmatch(r"[0-9a-f]{40}", secrets.hex_secret(20)())


def test_password_is_at_least_24_chars():
    assert len(secrets.password()) >= 24


def test_b64url_32_is_unpadded_and_decodes_to_32_bytes():
    value = secrets.b64url_32()
    assert "=" not in value
    padded = value + "=" * (-len(value) % 4)
    assert len(base64.urlsafe_b64decode(padded)) == 32
