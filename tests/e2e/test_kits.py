"""End-to-end checks for kits other than the forge, against live kits on this host.

    HITKIT_E2E=1 uv run pytest tests/e2e/test_kits.py -v

Each test skips unless its kit is up (`hitkit up netbird`, `hitkit up ludus`, `hitkit up pbs`). They
check that the kit works and runs the version this checkout pins, so they are the
test for a version bump.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import tomllib
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HITKIT_E2E") != "1", reason="set HITKIT_E2E=1 to run")

ROOT = Path(__file__).resolve().parents[2]
HITKIT = [str(ROOT / ".venv" / "bin" / "hitkit")]
IMAGES = ROOT / "src" / "hitkit" / "images"


def hitkit(*args: str):
    return json.loads(subprocess.run(HITKIT + ["--json", *args], capture_output=True, text=True, check=True).stdout)


def unmet(reason: str) -> None:
    """Skip locally; in local CI (HITKIT_E2E_REQUIRE=1), where the kits under test were just
    brought up, a missing one is a failure, not a pass."""
    if os.environ.get("HITKIT_E2E_REQUIRE") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


def require_kit(name: str) -> None:
    machines = [m for m in hitkit("status") if m["kit"] == name]
    if not machines or any(m["state"] != "running" for m in machines):
        unmet(f"{name} kit not up")


def call(url: str, headers: dict[str, str]):
    """GET over TLS verified by the host trust store (the hitkit CA must be trusted)."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# --- netbird -------------------------------------------------------------------


def test_netbird_runs_the_pinned_server_and_its_api_works():
    require_kit("netbird")
    compose = (IMAGES / "netbird/mkosi.extra/usr/lib/hitkit/netbird/compose.yml").read_text()
    pinned = re.search(r"netbirdio/netbird-server:([0-9.]+)@", compose).group(1)
    running = subprocess.run(HITKIT + ["ssh", "netbird", "--", "docker", "inspect", "-f", "{{.Config.Image}}",
                                       "netbird-server"], capture_output=True, text=True, check=True).stdout
    assert f"netbird-server:{pinned}@" in running

    creds = hitkit("creds", "netbird")
    owner = {"Authorization": f"Token {creds['api_token']}"}
    assert call(f"{creds['api_url']}/users/current", owner)["email"] == creds["email"]
    keys = call(f"{creds['api_url']}/setup-keys", owner)
    assert any(k["valid"] and not k["revoked"] for k in keys), keys


def test_netbird_token_gets_its_own_working_service_user():
    require_kit("netbird")
    creds = hitkit("creds", "netbird")
    name = f"e2e-{int(time.time())}"
    # The default role, admin: NetBird blocks `user`-role accounts from the API by default
    # (the account setting regular_users_view_blocked).
    tok = hitkit("token", "create", "netbird", "--name", name)
    try:
        owner = {"Authorization": f"Token {creds['api_token']}"}
        services = call(f"{creds['api_url']}/users?service_user=true", owner)
        assert any(u["name"] == name and u["role"] == "admin" for u in services), services
        # /users/current refuses every service user, so the token is tried on /groups.
        groups = call(f"{creds['api_url']}/groups", {"Authorization": f"Token {tok['token']}"})
        assert any(g["name"] == "All" for g in groups), groups
    finally:
        subprocess.run(HITKIT + ["token", "revoke", "netbird", name], check=True, capture_output=True)


# --- ludus ---------------------------------------------------------------------


def test_ludus_runs_the_pinned_version_and_its_api_works():
    require_kit("ludus")
    pins = tomllib.loads((IMAGES / "ludus" / "pins.toml").read_text())["artifact"]
    pinned = next(a["version"] for a in pins if a["name"] == "ludus")
    creds = hitkit("creds", "ludus")
    key = {"X-API-KEY": creds["api_key"]}

    version = call(f"{creds['url']}/api/v2/", key)
    assert pinned in version["version"], version
    me = call(f"{creds['url']}/api/v2/whoami", key)
    assert creds["user_id"] in json.dumps(me), me


# --- pbs -----------------------------------------------------------------------


def test_pbs_runs_the_pinned_version_and_its_api_works():
    require_kit("pbs")
    pinned = re.search(r"proxmox-backup-server=(\d+\.\d+\.\d+)", (IMAGES / "pbs" / "mkosi.conf").read_text()).group(1)
    creds = hitkit("creds", "pbs")
    header = dict([creds["api_header"].split(": ", 1)])

    version = call(f"{creds['url']}/api2/json/version", header)["data"]  # {"version": "4.2", "release": "8"}
    assert f"{version['version']}.{version['release']}" == pinned
    stores = call(f"{creds['url']}/api2/json/admin/datastore", header)["data"]
    assert "data" in [s["store"] for s in stores], stores


def test_pbs_storage_on_proxmox_when_wired():
    require_kit("pbs")
    require_kit("proxmox")
    pve = hitkit("creds", "proxmox")
    storages = call(f"{pve['url']}/api2/json/storage", dict([pve["api_header"].split(": ", 1)]))["data"]
    if "pbs" not in [s["storage"] for s in storages]:
        unmet("pbs not wired to proxmox (`hitkit up pbs --proxmox`)")
    status = call(f"{pve['url']}/api2/json/nodes/proxmox/storage/pbs/status", dict([pve["api_header"].split(": ", 1)]))
    assert status["data"]["active"] == 1, status


# --- incus-os ------------------------------------------------------------------
# The disruptive tests restart the machine (destroy and up again, restore, grow) and take
# minutes each; they also run with HITKIT_E2E_DISRUPTIVE=1. Nothing is deleted: destroy
# keeps the disk, restore keeps the current disk in vms/.kept.

disruptive = pytest.mark.skipif(os.environ.get("HITKIT_E2E_DISRUPTIVE") != "1",
                                reason="set HITKIT_E2E_DISRUPTIVE=1 to restart incus-os")


def incus_os_api():
    from hitkit import kits
    from hitkit.cli import _host_context

    return kits.hooks("incus-os").api_client(_host_context())


def incus_cli(conf: Path, *args: str) -> str:
    """The incus client with its own configuration, never yours."""
    env = dict(os.environ, INCUS_CONF=str(conf))
    return subprocess.run(["incus", *args], env=env, capture_output=True, text=True, check=True,
                          stdin=subprocess.DEVNULL).stdout


def test_incus_os_runs_the_pinned_version_with_secure_boot_and_tpm():
    require_kit("incus-os")
    pinned = tomllib.loads((IMAGES / "incus-os/installer.toml").read_text())["installer"]["version"]
    [machine] = [m for m in hitkit("status") if m["kit"] == "incus-os"]
    assert machine["versions"] == {"incus-os": pinned}
    api = incus_os_api()
    assert api.get("/1.0")["auth"] == "trusted"
    state = api.get("/os/1.0/system/security")["state"]
    assert state["secure_boot_enabled"] is True
    assert state["tpm_status"] == "ok"
    assert state["system_state_is_trusted"] is True
    assert {v["volume"]: v["state"] for v in state["encrypted_volumes"]}["root"] == "unlocked (TPM)"


def test_incus_os_creds_and_the_pinned_certificate():
    require_kit("incus-os")
    creds = hitkit("creds", "incus-os")
    assert creds["url"] == "https://incus-os.hitkit.internal:8443"
    assert creds["ui"] == creds["url"] + "/ui/"
    assert creds["encryption_recovery_key"] and creds["pool_recovery_key"]
    assert Path(creds["ui_certificate"]).is_file() and creds["ui_certificate_password"]
    from hitkit import incus

    served = incus.server_certificate("incus-os.hitkit.internal")
    assert incus.fingerprint(served) == creds["server_fingerprint"]


def test_incus_os_token_becomes_a_remote_that_launches_a_container(tmp_path):
    require_kit("incus-os")
    if not shutil.which("incus"):
        unmet("the incus CLI is not installed on this host")
    name = f"e2e-{int(time.time())}"
    tok = hitkit("token", "create", "incus-os", "--name", name)
    try:
        assert {t["name"]: t["scopes"] for t in hitkit("token", "list", "incus-os")}[name] == ["pending token"]
        incus_cli(tmp_path, "remote", "add", "hitkit-incus-os", tok["token"])  # no fingerprint prompt
        assert "https://incus-os.hitkit.internal:8443" in incus_cli(tmp_path, "remote", "list", "-f", "csv")
        incus_cli(tmp_path, "launch", "images:alpine/3.22", f"hitkit-incus-os:{name}")
        try:
            assert incus_cli(tmp_path, "exec", f"hitkit-incus-os:{name}", "--", "cat", "/etc/alpine-release")
        finally:
            incus_cli(tmp_path, "delete", "-f", f"hitkit-incus-os:{name}")
        assert {t["name"]: t["scopes"] for t in hitkit("token", "list", "incus-os")}[name] == ["trusted client"]
    finally:
        subprocess.run(HITKIT + ["token", "revoke", "incus-os", name], capture_output=True)
    assert name not in {t["name"] for t in hitkit("token", "list", "incus-os")}


def _marker_volume(api, name: str, create: bool) -> None:
    if create:
        api.post("/1.0/storage-pools/local/volumes", {"name": name, "type": "custom", "config": {}})
    else:
        api.delete(f"/1.0/storage-pools/local/volumes/custom/{name}")


def _volumes(api) -> set[str]:
    return {v.rsplit("/", 1)[-1] for v in api.get("/1.0/storage-pools/local/volumes/custom") or []}


def _pool_gib(api) -> float:
    [pool] = [p for p in api.get("/os/1.0/system/storage")["config"]["pools"] if p["name"] == "local"]
    return pool["usable_pool_size_in_bytes"] / 2**30


@disruptive
def test_incus_os_comes_back_from_its_kept_disk_without_a_recovery_key():
    require_kit("incus-os")
    api = incus_os_api()
    marker = f"e2e-kept-{int(time.time())}"
    _marker_volume(api, marker, create=True)
    fingerprint = hitkit("creds", "incus-os")["server_fingerprint"]
    subprocess.run(HITKIT + ["destroy", "incus-os", "--yes"], check=True, capture_output=True)
    subprocess.run(HITKIT + ["up", "incus-os"], check=True, capture_output=True)  # waits until trusted again
    api = incus_os_api()
    assert marker in _volumes(api)  # same disk, not a reinstall
    assert hitkit("creds", "incus-os")["server_fingerprint"] == fingerprint
    state = api.get("/os/1.0/system/security")["state"]
    assert {v["volume"]: v["state"] for v in state["encrypted_volumes"]}["root"] == "unlocked (TPM)"
    _marker_volume(api, marker, create=False)


@disruptive
def test_incus_os_backup_and_restore():
    require_kit("incus-os")
    api = incus_os_api()
    marker = f"e2e-backup-{int(time.time())}"
    _marker_volume(api, marker, create=True)
    subprocess.run(HITKIT + ["backup", "incus-os"], check=True, capture_output=True)
    _marker_volume(api, marker, create=False)
    assert marker not in _volumes(api)
    subprocess.run(HITKIT + ["restore", "incus-os", "--yes"], check=True, capture_output=True)
    api = incus_os_api()
    assert marker in _volumes(api)
    _marker_volume(api, marker, create=False)


@disruptive
def test_incus_os_disk_grows_into_the_pool_after_a_reboot():
    require_kit("incus-os")
    [machine] = [m for m in hitkit("status") if m["kit"] == "incus-os"]
    before = _pool_gib(incus_os_api())
    target = machine["disk_gib"] + 10
    subprocess.run(HITKIT + ["up", "incus-os", "--disk", str(target)], check=True, capture_output=True)
    subprocess.run(HITKIT + ["down", "incus-os"], check=True, capture_output=True)
    subprocess.run(HITKIT + ["up", "incus-os"], check=True, capture_output=True)
    assert _pool_gib(incus_os_api()) > before + 9
