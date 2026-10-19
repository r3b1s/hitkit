"""ludus kit: definition, image role, and creating the human's Ludus admin over SSH."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hitkit import kits, secrets
from hitkit.errors import HitkitError
from hitkit.machine import images


def test_ludus_builds_on_the_proxmox_image_and_pins_its_binaries():
    dirs = [d.name for d in images.role_dirs("ludus")]
    assert "proxmox" in dirs
    names = {a.name: a for a in images.pinned_artifacts(images.role_dirs("ludus"))}
    assert names["ludus"].version == "2.3.8" and len(names["ludus"].sha256) == 64
    assert names["ludus-client"].dest == "/usr/local/bin/ludus"


def _module():
    import sys

    return sys.modules[type(kits.hooks("ludus")).__module__]


@pytest.mark.parametrize("name,expected", [("t", "t"), ("jane.doe", "janedoe"), ("root", "hitkit"),
                                           ("a" * 30, "a" * 20), ("---", "hitkit")])
def test_user_ids_satisfy_ludus_rules(name, expected):
    assert _module().user_id(name) == expected


@pytest.fixture
def ludus(monkeypatch, user):
    h = kits.hooks("ludus")
    remote_module = h.after_ready.__globals__["remote"]
    state = SimpleNamespace(calls=[], replies=[], hooks=h)

    def fake_run(hctx, machine, command, stdin=None):
        state.calls.append((command, stdin))
        return state.replies.pop(0)

    monkeypatch.setattr(remote_module, "run", fake_run)
    state.hctx = SimpleNamespace(user=user)
    state.machine = SimpleNamespace(name="ludus")
    return state


def test_bring_up_creates_an_admin_and_keeps_its_key(ludus, user):
    ludus.replies = [json.dumps({"userID": "t", "apiKey": "t._key_", "isAdmin": True})]

    ludus.hooks.after_ready(ludus.hctx, ludus.machine, (), {}, lambda m: None)

    command, body = ludus.calls[0]
    assert "127.0.0.1" in command and ":8081/api/v2/user" in command
    assert json.loads(body)["isAdmin"] is True
    assert json.loads(body)["password"] not in command  # secrets via stdin only
    assert secrets.get(user, "ludus", "api.key") == "t._key_"


def test_an_existing_admin_gets_a_fresh_key(ludus, user):
    ludus.replies = ['{"error": "User with that ID already exists"}',
                     json.dumps({"result": {"apiKey": "t._new_", "userID": "t"}})]

    ludus.hooks.after_ready(ludus.hctx, ludus.machine, (), {}, lambda m: None)

    assert "/user/apikey?userID=" in ludus.calls[1][0]
    assert secrets.get(user, "ludus", "api.key") == "t._new_"


def test_bring_up_is_skipped_once_a_key_is_stored(ludus, user):
    secrets.put(user, "ludus", "api.key", "kept")

    ludus.hooks.after_ready(ludus.hctx, ludus.machine, (), {}, lambda m: None)

    assert ludus.calls == []


def test_creds_before_bring_up_raises(ludus):
    with pytest.raises(HitkitError, match="not been brought up"):
        ludus.hooks.creds(ludus.hctx)
