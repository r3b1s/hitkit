"""pbs kit: definition, image role, and bring-up / Proxmox VE wiring over (monkeypatched) SSH."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from hitkit import kits, secrets
from hitkit.addressing import slot
from hitkit.errors import HitkitError
from hitkit.machine import images

ROLE_DIR = images.IMAGES_DIR / "pbs"


def test_kit_has_one_stateful_pbs_machine_on_its_slot():
    kit = kits.load("pbs")
    (machine,) = kit.machines
    assert (machine.name, machine.role, machine.stateful_os) == ("pbs", "pbs", True)
    assert slot("pbs").host_octet == 12


def test_proxmox_knob_defaults_off_and_parses():
    kit = kits.load("pbs")
    assert kit.default_knobs() == {"proxmox": False}
    assert kit.parse_knobs(["--proxmox"]) == {"proxmox": True}


def test_role_builds_on_base_and_pins_the_server_and_keyring():
    assert [d.name for d in images.role_dirs("pbs")][-2:] == ["base", "pbs"]
    conf = (ROLE_DIR / "mkosi.conf").read_text()
    assert re.search(r"^\s+proxmox-backup-server=\d+\.\d+\.\d+-\d+$", conf, re.M)
    assert re.search(r"^\s+proxmox-archive-keyring=\d+(\.\d+)*$", conf, re.M)


def test_build_and_installed_repository_are_the_trixie_no_subscription_one():
    for sources in (ROLE_DIR / "mkosi.sandbox/etc/apt/sources.list.d/pbs.sources",
                    ROLE_DIR / "mkosi.extra/etc/apt/sources.list.d/pbs.sources"):
        text = sources.read_text()
        assert "URIs: http://download.proxmox.com/debian/pbs" in text
        assert "Suites: trixie" in text and "Components: pbs-no-subscription" in text
    assert (ROLE_DIR / "mkosi.sandbox/etc/apt/keyrings/proxmox.gpg").is_file()


def test_enterprise_repository_is_kept_but_disabled():
    script = (ROLE_DIR / "mkosi.postinst.chroot").read_text()
    assert "pbs-enterprise.sources" in script and "Enabled: false" in script


# --- hooks -----------------------------------------------------------------------


@pytest.fixture
def hctx(user):
    return SimpleNamespace(user=user)


@pytest.fixture
def pbs(monkeypatch):
    """Fakes remote.run: records (machine, command, stdin); the datastore list and token JSON are canned."""
    hooks = kits.hooks("pbs")
    remote_module = hooks.after_ready.__globals__["remote"]
    state = SimpleNamespace(hooks=hooks, calls=[], datastores=[], pve_storages="0\n", tokens=iter(f"secret-{n}" for n in range(9)))

    def fake_run(hctx, machine, command, stdin=None):
        state.calls.append((machine, command, stdin))
        if "datastore list" in command:
            return json.dumps([{"name": n} for n in state.datastores])
        if "generate-token" in command:
            assert "--output-format" not in command  # generate-token rejects it
            return "Result: " + json.dumps({"tokenid": "x", "value": next(state.tokens)}, indent=2) + "\n"
        if "storage.cfg" in command:
            return state.pve_storages
        return ""

    monkeypatch.setattr(remote_module, "run", fake_run)
    state.commands = lambda machine="pbs": [c for m, c, _ in state.calls if m == machine]
    return state


def test_root_password_is_generated_once_and_then_stable(hctx):
    hooks = kits.hooks("pbs")

    first = hooks.credentials(hctx, None, ())

    assert set(first) == {"hitkit.pbs.root-password"}
    assert hooks.credentials(hctx, None, ()) == first


def test_creds_before_bring_up_raises(hctx):
    with pytest.raises(HitkitError, match="not been brought up"):
        kits.hooks("pbs").creds(hctx)


def test_bring_up_installs_cert_creates_datastore_and_stores_an_admin_token(pbs, user, hctx):
    pbs.hooks.after_ready(hctx, SimpleNamespace(name="pbs"), (), {}, lambda m: None)

    cmds = pbs.commands()
    assert "/etc/proxmox-backup/proxy.pem" in cmds[0] and "systemctl restart proxmox-backup-proxy" in cmds[0]
    assert any("datastore create data /srv/data/pbs" in c for c in cmds)
    assert any("acl update / Admin --auth-id 'root@pam!hitkit'" in c for c in cmds)
    assert secrets.get(user, "pbs", "api.token") == "root@pam!hitkit:secret-0"


def test_bring_up_keeps_an_existing_datastore_and_token(pbs, user, hctx):
    pbs.datastores = ["data"]
    secrets.put(user, "pbs", "api.token", "root@pam!hitkit:kept")

    pbs.hooks.after_ready(hctx, SimpleNamespace(name="pbs"), (), {}, lambda m: None)

    assert not any("datastore create" in c or "generate-token" in c for c in pbs.commands())
    assert secrets.get(user, "pbs", "api.token") == "root@pam!hitkit:kept"


def test_creds_describe_the_api_and_the_backup_repository(user, hctx):
    secrets.put(user, "pbs", "root.password", "pw")
    secrets.put(user, "pbs", "api.token", "root@pam!hitkit:tok")

    info = kits.hooks("pbs").creds(hctx)

    assert info["url"] == "https://pbs.hitkit.internal:8007"
    assert info["api_header"] == "Authorization: PBSAPIToken=root@pam!hitkit:tok"
    assert info["repository"] == "root@pam!hitkit@pbs.hitkit.internal:data"


def test_without_the_proxmox_knob_proxmox_is_left_alone(pbs, hctx):
    pbs.hooks.finish(hctx, (), {"proxmox": False}, lambda m: None)

    assert pbs.calls == []


def test_proxmox_knob_needs_the_proxmox_kit(pbs, hctx):
    with pytest.raises(HitkitError, match="proxmox kit") as err:
        pbs.hooks.finish(hctx, (), {"proxmox": True}, lambda m: None)
    assert "hitkit up proxmox" in err.value.hint


def test_proxmox_knob_adds_the_datastore_as_storage_with_a_backup_only_token(pbs, user, hctx):
    secrets.put(user, "proxmox", "api.token", "root@pam!hitkit=tok")

    pbs.hooks.finish(hctx, (), {"proxmox": True}, lambda m: None)

    on_pbs = pbs.commands("pbs")
    assert any("user create pve@pbs" in c for c in on_pbs)
    for auth_id in ("pve@pbs", "pve@pbs!hitkit"):  # the token is capped by its user's permissions
        assert any(f"acl update /datastore/data DatastorePowerUser --auth-id '{auth_id}'" in c for c in on_pbs)
    (add,) = [(c, s) for m, c, s in pbs.calls if m == "proxmox" and "pvesm" in c]
    command, stdin = add
    assert "pvesm add pbs pbs --server pbs.hitkit.internal --datastore data" in command
    assert "--username 'pve@pbs!hitkit'" in command and "--content backup" in command
    assert stdin == "secret-0\n" and "secret-0" not in command  # the secret goes via stdin only
    assert secrets.get(user, "pbs", "pve.token") == "secret-0"


def test_proxmox_knob_is_repeatable_and_refreshes_an_existing_storage(pbs, user, hctx):
    secrets.put(user, "proxmox", "api.token", "root@pam!hitkit=tok")
    secrets.put(user, "pbs", "pve.token", "stored")
    pbs.pve_storages = "1\n"

    pbs.hooks.finish(hctx, (), {"proxmox": True}, lambda m: None)

    assert not any("generate-token" in c for c in pbs.commands("pbs"))  # the token is kept
    assert sum("acl update" in c for c in pbs.commands("pbs")) == 2  # permissions are re-asserted
    (command, stdin) = next((c, s) for m, c, s in pbs.calls if "pvesm" in c)
    assert "pvesm set pbs --server" in command and stdin == "stored\n"
