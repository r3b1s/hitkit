"""Forge kit: push mirrors, collaborator grants, mirror username defaults and cache credentials."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hitkit import kits
from hitkit.errors import HitkitError
from hitkit.machine.spec import MachinePaths

# Module-level helpers live in hooks.py, which kits.hooks() executes outside sys.modules.
MOD = SimpleNamespace(**kits.hooks("forge").credentials.__globals__)


class RecordingAdmin:
    """Stands in for the forge admin API: records every call and returns canned GET data."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.get_result = []

    def get(self, path):
        self.calls.append(("GET", path, None))
        return self.get_result

    def post(self, path, body=None):
        self.calls.append(("POST", path, body))
        return {}

    def put(self, path, body=None):
        self.calls.append(("PUT", path, body))

    def delete(self, path, ok=(204, 404)):
        self.calls.append(("DELETE", path, None))


@pytest.fixture
def forge(monkeypatch, user):
    hooks = kits.hooks("forge")
    admin = RecordingAdmin()
    monkeypatch.setattr(type(hooks), "admin_api", lambda self, hctx: admin)
    return SimpleNamespace(hooks=hooks, admin=admin, hctx=SimpleNamespace(user=user))


@pytest.mark.parametrize(
    "remote, expected",
    [
        ("https://github.com/a/b.git", "x-access-token"),
        ("https://gitlab.com/a/b.git", "oauth2"),
        ("https://gitlab.example.org/a/b.git", "oauth2"),
    ],
)
def test_default_mirror_username_for_token_auth_forges(remote, expected):
    assert MOD.default_mirror_username(remote) == expected


def test_default_mirror_username_refuses_unknown_forges():
    with pytest.raises(HitkitError, match="cannot guess the username") as err:
        MOD.default_mirror_username("https://codeberg.org/a/b.git")
    assert err.value.hint == "pass --username"


def test_mirror_add_posts_the_mirror_with_the_defaulted_username(forge):
    forge.hooks.mirror_add(forge.hctx, "o/r", "https://github.com/a/b.git", None, "pw", "8h", True)

    assert forge.admin.calls == [("POST", "/repos/o/r/push_mirrors", {
        "remote_address": "https://github.com/a/b.git", "remote_username": "x-access-token",
        "remote_password": "pw", "interval": "8h", "sync_on_commit": True,
    })]


def test_mirror_add_uses_an_explicit_username(forge):
    forge.hooks.mirror_add(forge.hctx, "o/r", "https://codeberg.org/a/b.git", "bot", "pw", "1h", False)

    body = forge.admin.calls[0][2]
    assert body["remote_username"] == "bot"
    assert body["sync_on_commit"] is False


def test_mirror_list_maps_forgejo_fields_and_blanks_errors_to_none(forge):
    forge.admin.get_result = [
        {"remote_name": "m1", "remote_address": "https://x/y", "interval": "8h", "last_update": "t1",
         "last_error": "", "sync_on_commit": True},
        {"remote_name": "m2", "remote_address": "https://x/z", "interval": "1h", "last_update": "t2",
         "last_error": "auth failed", "sync_on_commit": False},
    ]

    assert forge.hooks.mirror_list(forge.hctx, "o/r") == [
        {"name": "m1", "remote": "https://x/y", "interval": "8h", "last_update": "t1",
         "last_error": None, "sync_on_commit": True},
        {"name": "m2", "remote": "https://x/z", "interval": "1h", "last_update": "t2",
         "last_error": "auth failed", "sync_on_commit": False},
    ]
    assert forge.admin.calls == [("GET", "/repos/o/r/push_mirrors", None)]


def test_mirror_remove_deletes_by_mirror_name(forge):
    forge.hooks.mirror_remove(forge.hctx, "o/r", "m1")
    assert forge.admin.calls == [("DELETE", "/repos/o/r/push_mirrors/m1", None)]


def test_mirror_sync_triggers_the_repo_sync_endpoint(forge):
    forge.hooks.mirror_sync(forge.hctx, "o/r")
    assert forge.admin.calls == [("POST", "/repos/o/r/push_mirrors-sync", None)]


def test_grant_sets_the_collaborator_permission(forge):
    forge.hooks.grant(forge.hctx, "o/r", "hitkit-dev", "write")
    assert forge.admin.calls == [("PUT", "/repos/o/r/collaborators/hitkit-dev", {"permission": "write"})]


def test_cache_credentials_only_when_the_cache_machine_is_known(user):
    hctx = SimpleNamespace(paths=lambda name: MachinePaths.for_machine(user.data_dir / "vms", name))
    assert MOD.cache_credentials(hctx) == {}

    known = hctx.paths("cache").known_hosts
    known.parent.mkdir(parents=True)
    known.write_text("cache host key")

    assert MOD.cache_credentials(hctx) == {"hitkit.runner.cache": b"https://cache.hitkit.internal"}
