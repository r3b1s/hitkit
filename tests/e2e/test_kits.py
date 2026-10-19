"""End-to-end checks for kits other than the forge, against live kits on this host.

    HITKIT_E2E=1 uv run pytest tests/e2e/test_kits.py -v

Each test skips unless its kit is up (`hitkit up netbird`, `hitkit up ludus`). They
check that the kit works and runs the version this checkout pins, so they are the
test for a version bump.
"""

from __future__ import annotations

import json
import os
import re
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


def require_kit(name: str) -> None:
    machines = [m for m in hitkit("status") if m["kit"] == name]
    if not machines or any(m["state"] != "running" for m in machines):
        pytest.skip(f"{name} kit not up")


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
