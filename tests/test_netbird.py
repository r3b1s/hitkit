"""netbird kit: definition, image role, and claiming/administering the instance over its API."""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from types import SimpleNamespace

import pytest

from hitkit import kits, secrets
from hitkit.addressing import slot
from hitkit.errors import HitkitError
from hitkit.machine import creds as machine_creds
from hitkit.machine import images

ROLE_DIR = images.IMAGES_DIR / "netbird"


def _module():
    return sys.modules[type(kits.hooks("netbird")).__module__]


def test_kit_has_one_netbird_machine_on_its_slot():
    kit = kits.load("netbird")
    assert [(m.name, m.role) for m in kit.machines] == [("netbird", "netbird")]
    assert slot("netbird").host_octet == 70
    assert kit.machines[0].ready_timeout >= 600  # the first boot pulls the images


def test_role_builds_on_base_and_pins_every_image_by_digest():
    assert [d.name for d in images.role_dirs("netbird")][-2:] == ["base", "netbird"]
    compose = (ROLE_DIR / "mkosi.extra/usr/lib/hitkit/netbird/compose.yml").read_text()
    refs = re.findall(r"^\s+image: (\S+)$", compose, re.MULTILINE)
    assert len(refs) == 3
    assert all(re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", r) for r in refs)
    assert "NB_SETUP_PAT_ENABLED" in compose and "certresolver" not in compose


def test_config_is_rendered_from_the_kit_credentials():
    script = (ROLE_DIR / "mkosi.extra/usr/lib/hitkit/provision.d/50-netbird").read_text()
    cred_names = set(re.findall(r"\$c/(hitkit\.nb\.[\w-]+)", script))
    assert cred_names == {"hitkit.nb.relay-secret", "hitkit.nb.cookie-key", "hitkit.nb.store-key"}


def test_credentials_are_stable_and_fit_fw_cfg(user):
    h, hctx = kits.hooks("netbird"), SimpleNamespace(user=user)
    first = h.credentials(hctx, None, ())
    assert set(first) == {"hitkit.nb.store-key", "hitkit.nb.cookie-key", "hitkit.nb.relay-secret"}
    assert all(len(n) <= machine_creds.MAX_NAME for n in first)
    assert h.credentials(hctx, None, ()) == first
    import base64
    assert len(base64.b64decode(first["hitkit.nb.store-key"], validate=True)) == 32


class FakeNetBird:
    """Just enough of the NetBird API: instance setup, users, tokens, setup keys."""

    def __init__(self):
        self.setup_required = True
        self.users: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}  # plain token -> {id, name, user}
        self.keys: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"id{self._n}"

    def _pat(self, user_id: str, name: str) -> dict:
        plain = f"nbp_{self._id()}_secret"
        self.tokens[plain] = {"id": self._id(), "name": name, "user": user_id,
                              "expiration_date": "2027-10-13T00:00:00Z"}
        return {"plain_token": plain, "personal_access_token": self.tokens[plain]}

    def request(self, api, method, path, body=None, ok=(200, 201, 204)):
        nb = _module()
        self.calls.append((method, path))
        if path == "/instance":
            return {"setup_required": self.setup_required}
        if path == "/setup":
            assert self.setup_required and body["create_pat"] and body["pat_expire_in"] == 365
            self.setup_required = False
            uid = self._id()
            self.users[uid] = {"id": uid, "name": body["name"], "role": "owner", "is_service_user": False}
            return {"user_id": uid, "email": body["email"],
                    "personal_access_token": self._pat(uid, "setup-token")["plain_token"]}
        me = self.tokens.get(api.token)
        if me is None:
            raise nb.NetBirdError(401, "token invalid")
        if path == "/users/current":
            return self.users[me["user"]]
        if path == "/users?service_user=true":
            return [u for u in self.users.values() if u["is_service_user"]]
        if path == "/users" and method == "POST":
            uid = self._id()
            self.users[uid] = {"id": uid, "name": body["name"], "role": body["role"], "is_service_user": True}
            return self.users[uid]
        if m := re.fullmatch(r"/users/(\w+)/tokens", path):
            if method == "POST":
                return self._pat(m.group(1), body["name"])
            return [t for t in self.tokens.values() if t["user"] == m.group(1)]
        if m := re.fullmatch(r"/users/(\w+)/tokens/(\w+)", path):
            self.tokens = {p: t for p, t in self.tokens.items() if t["id"] != m.group(2)}
            return None
        if m := re.fullmatch(r"/users/(\w+)", path):
            del self.users[m.group(1)]
            self.tokens = {p: t for p, t in self.tokens.items() if t["user"] != m.group(1)}
            return None
        if path == "/setup-keys" and method == "POST":
            assert body["type"] == "reusable" and body["expires_in"] == 0
            self.keys.append({"name": body["name"], "valid": True, "revoked": False})
            return {"key": f"KEY-{len(self.keys)}"}
        if path == "/setup-keys":
            return self.keys
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def nb(monkeypatch, user):
    fake = FakeNetBird()
    monkeypatch.setattr(_module().NetBird, "request",
                        lambda api, method, path, body=None, ok=(200, 201, 204): fake.request(api, method, path, body))
    fake.hooks = kits.hooks("netbird")
    fake.hctx = SimpleNamespace(user=user)
    fake.up = lambda: fake.hooks.after_ready(fake.hctx, SimpleNamespace(name="netbird"), (), {}, lambda m: None)
    return fake


def test_bring_up_claims_the_instance_and_creates_a_setup_key(nb, user):
    nb.up()
    info = nb.hooks.creds(nb.hctx)
    assert info["email"] == f"{user.name}@hitkit.internal"
    assert info["password"] == secrets.get(user, "netbird", "owner.password")
    assert info["api_token"] in nb.tokens and info["api_header"] == f"Authorization: Token {info['api_token']}"
    assert info["setup_key"] == "KEY-1"


def test_second_bring_up_changes_nothing(nb):
    nb.up()
    before = (nb.hooks.creds(nb.hctx), len(nb.tokens), len(nb.keys))
    nb.calls.clear()
    nb.up()
    assert (nb.hooks.creds(nb.hctx), len(nb.tokens), len(nb.keys)) == before
    assert not [c for c in nb.calls if c[0] != "GET"]


def test_revoked_setup_key_is_replaced(nb):
    nb.up()
    nb.keys[0]["revoked"] = True
    nb.up()
    assert nb.hooks.creds(nb.hctx)["setup_key"] == "KEY-2"


def test_expiring_token_is_renewed_and_the_old_one_deleted(nb, user):
    nb.up()
    old = secrets.get(user, "netbird", "admin.token")
    soon = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    secrets.put(user, "netbird", "admin.token.json", json.dumps({"expires": soon}))
    nb.up()
    new = secrets.get(user, "netbird", "admin.token")
    assert new != old and old not in nb.tokens and nb.tokens[new]["name"] == "hitkit"
    assert secrets.get(user, "netbird", "admin.token.previous") == old


def test_restored_backup_falls_back_to_the_previous_token(nb, user):
    nb.up()
    old = secrets.get(user, "netbird", "admin.token")
    secrets.put(user, "netbird", "admin.token.previous", old)
    secrets.put(user, "netbird", "admin.token", "nbp_unknown_to_the_restored_db")
    nb.up()
    assert secrets.get(user, "netbird", "admin.token") in nb.tokens  # renewed from the old one


def test_set_up_instance_without_a_valid_token_is_an_error(nb, user):
    nb.setup_required = False
    with pytest.raises(HitkitError, match="no valid API token"):
        nb.up()


def test_tokens_are_service_users_with_a_role(nb):
    nb.up()
    made = nb.hooks.token_create(nb.hctx, "agent", ["user"])
    assert made["token"] in nb.tokens and made["scopes"] == ["user"]
    assert [t["name"] for t in nb.hooks.token_list(nb.hctx)] == ["agent"]
    with pytest.raises(HitkitError, match="already exists"):
        nb.hooks.token_create(nb.hctx, "agent", None)
    with pytest.raises(HitkitError, match="one --scope"):
        nb.hooks.token_create(nb.hctx, "other", ["write:repository"])
    nb.hooks.token_revoke(nb.hctx, "agent")
    assert nb.hooks.token_list(nb.hctx) == [] and made["token"] not in nb.tokens


def test_creds_before_bring_up_raises(nb):
    with pytest.raises(HitkitError, match="not been brought up"):
        nb.hooks.creds(nb.hctx)
