"""hitkit-dev and proxmox kit hooks: credentials, bring-up over SSH, and the API token."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hitkit import kits, secrets
from hitkit.errors import HitkitError


@pytest.fixture
def hctx(user):
    return SimpleNamespace(user=user)


# --- hitkit-dev -----------------------------------------------------------------


def test_distro_dev_kits_reuse_the_hitkit_dev_hooks_class():
    dev = type(kits.hooks("hitkit-dev"))
    assert type(kits.hooks("hitkit-dev-debian")) is dev
    assert type(kits.hooks("hitkit-dev-fedora")) is dev


def test_dev_gets_no_credentials_without_a_forge_admin_token(hctx):
    assert kits.hooks("hitkit-dev").credentials(hctx, None, ()) == {}


def test_dev_keeps_its_stored_token_when_the_forge_is_unreachable(monkeypatch, user, hctx):
    secrets.put(user, "forge", "admin.token", "admin")
    secrets.put(user, "forge", "hitkit-dev.token", "dev-token")
    forge_cls = type(kits.hooks("forge"))

    def unreachable(self, hctx):
        raise HitkitError("cannot reach Forgejo")

    def must_not_mint(*args, **kwargs):
        raise AssertionError("the stored token must be reused, not minted again")

    monkeypatch.setattr(forge_cls, "admin_api", unreachable)
    monkeypatch.setattr(forge_cls, "_mint_token", staticmethod(must_not_mint))

    creds = kits.hooks("hitkit-dev").credentials(hctx, None, ())

    assert creds == {"hitkit.dev.forge-user": b"hitkit-dev", "hitkit.dev.forge-token": b"dev-token"}


# --- proxmox: credentials and the user-facing description -------------------------


def test_proxmox_root_password_is_generated_once_and_then_stable(hctx):
    hooks = kits.hooks("proxmox")

    first = hooks.credentials(hctx, None, ())

    assert set(first) == {"hitkit.pve.root-password"}
    assert hooks.credentials(hctx, None, ()) == first


def test_proxmox_creds_before_bring_up_raises(hctx):
    with pytest.raises(HitkitError, match="not been brought up"):
        kits.hooks("proxmox").creds(hctx)


def test_proxmox_creds_after_bring_up_describe_the_api(user, hctx):
    secrets.put(user, "proxmox", "root.password", "pw")
    secrets.put(user, "proxmox", "api.token", "root@pam!hitkit=tok")

    info = kits.hooks("proxmox").creds(hctx)

    assert info["url"] == "https://proxmox.hitkit.internal:8006"
    assert info["username"] == "root@pam"
    assert info["password"] == "pw"
    assert info["api_header"].startswith("Authorization: PVEAPIToken=")


# --- proxmox: after_ready over (monkeypatched) SSH ----------------------------------


@pytest.fixture
def pve(monkeypatch):
    """Fakes remote.run: storage.cfg holds the given `dir:` storages, the token add returns a value."""
    hooks = kits.hooks("proxmox")
    remote_module = hooks.after_ready.__globals__["remote"]
    state = SimpleNamespace(hooks=hooks, cmds=[], storages="local\n")

    def fake_run(hctx, machine, command, stdin=None):
        state.cmds.append(command)
        if "/etc/pve/storage.cfg" in command:  # grep -c '^dir: data$'
            return "1\n" if "data" in state.storages.split() else "0\n"
        if "pveum user token add" in command:
            return json.dumps({"value": "tok"})
        return ""

    monkeypatch.setattr(remote_module, "run", fake_run)
    return state


def test_proxmox_bring_up_installs_cert_adds_storage_and_stores_the_token(pve, user, hctx):
    machine = SimpleNamespace(name="proxmox", role="proxmox")

    pve.hooks.after_ready(hctx, machine, (), {}, lambda message: None)

    assert pve.cmds[0].startswith(
        "cp /etc/hitkit/tls/server.crt /etc/pve/local/pveproxy-ssl.pem && "
        "cp /etc/hitkit/tls/server.key /etc/pve/local/pveproxy-ssl.key"
    )
    assert not any("install -m" in cmd for cmd in pve.cmds)  # pmxcfs rejects chmod
    assert any("pvesm add dir data --path /srv/data/pve" in cmd for cmd in pve.cmds)
    assert secrets.get(user, "proxmox", "api.token") == "root@pam!hitkit=tok"


def test_proxmox_bring_up_does_not_readd_existing_data_storage(pve, hctx):
    # Regression: `pvesm status` can omit an inactive storage; storage.cfg is authoritative.
    pve.storages = "local\ndata\n"
    machine = SimpleNamespace(name="proxmox", role="proxmox")

    pve.hooks.after_ready(hctx, machine, (), {}, lambda message: None)

    assert not any("pvesm add" in cmd for cmd in pve.cmds)
