"""End-to-end checks for the post-MVP phases, against live kits on this host.

    HITKIT_E2E=1 uv run pytest tests/e2e -v

Needs `hitkit up forge --ephemeral 1` (or more). The cache and proxmox tests
skip unless `hitkit up cache` / `hitkit up proxmox` have been run.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HITKIT_E2E") != "1", reason="set HITKIT_E2E=1 to run")

HITKIT = [str(Path(__file__).resolve().parents[2] / ".venv" / "bin" / "hitkit"), "--json"]


def hitkit(*args: str):
    return json.loads(subprocess.run(HITKIT + list(args), capture_output=True, text=True, check=True).stdout)


@pytest.fixture(scope="module")
def ctx():
    from hitkit import kits
    from hitkit.cli import _host_context

    hctx = _host_context()
    admin = kits.hooks("forge").admin_api(hctx)
    user = hitkit("creds", "forge")["username"]
    name = f"later-{int(time.time())}"
    admin.post(f"/admin/users/{user}/repos", {"name": name, "default_branch": "main", "auto_init": True})
    return {"hctx": hctx, "admin": admin, "user": user, "repo": f"{user}/{name}"}


def put_file(api, repo: str, path: str, content: str) -> None:
    api.post(f"/repos/{repo}/contents/{path}", {
        "content": base64.b64encode(content.encode()).decode(), "message": f"add {path}"})


def run_workflow(api, repo: str, workflow: str, timeout: float = 600) -> dict:
    time.sleep(2)
    before = {r["id"] for r in api.get(f"/repos/{repo}/actions/runs")["workflow_runs"]}
    api.post(f"/repos/{repo}/actions/workflows/{workflow}/dispatches", {"ref": "main"})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = [r for r in api.get(f"/repos/{repo}/actions/runs")["workflow_runs"] if r["id"] not in before]
        if runs and runs[0]["status"] not in ("waiting", "running"):
            return runs[0]
        time.sleep(5)
    pytest.fail(f"{workflow} did not finish within {timeout}s")


def test_git_push_over_ssh_with_pinned_host_key(ctx, tmp_path):
    work = tmp_path / "clone"
    url = f"forgejo.hitkit.internal:{ctx['repo']}.git"  # resolved via the managed ~/.ssh/config include
    subprocess.run(["git", "clone", "-q", url, str(work)], check=True, env=os.environ | {"GIT_SSH_COMMAND": "ssh -o BatchMode=yes"})
    (work / "ssh.txt").write_text("over ssh\n")
    git = ["git", "-C", str(work), "-c", "user.name=e2e", "-c", "user.email=e2e@hitkit.internal"]
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "ssh push"], check=True)
    subprocess.run(git + ["push", "-q", "origin", "HEAD"], check=True,
                   env=os.environ | {"GIT_SSH_COMMAND": "ssh -o BatchMode=yes"})
    head = ctx["admin"].get(f"/repos/{ctx['repo']}/branches/main")["commit"]["message"]
    assert head.strip() == "ssh push"


def test_push_mirror_reaches_the_remote(ctx):
    admin, repo = ctx["admin"], ctx["repo"]
    target = f"{repo}-mirror"
    admin.post(f"/admin/users/{ctx['user']}/repos", {"name": target.split("/")[1]})
    token = hitkit("token", "create", "forge", "--name", f"mirror-{int(time.time())}",
                   "--scope", "write:repository")["token"]
    remote = f"https://forgejo.hitkit.internal/{target}.git"
    subprocess.run(HITKIT[:1] + ["mirror", "add", repo, remote, "--username", ctx["user"], "--interval", "1h0m0s"],
                   check=True, env=os.environ | {"HITKIT_MIRROR_TOKEN": token}, capture_output=True)
    subprocess.run(HITKIT[:1] + ["mirror", "sync", repo], check=True, capture_output=True)
    source = admin.get(f"/repos/{repo}/branches/main")["commit"]["id"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if admin.get(f"/repos/{target}/branches/main")["commit"]["id"] == source:
                return
        except Exception:
            pass
        time.sleep(5)
    pytest.fail("mirror target never received the source head")


def test_ephemeral_job_runs_in_a_fresh_vm(ctx):
    from hitkit import forgepool

    cfg = forgepool.load_config(ctx["hctx"].user) or {}
    if not cfg.get("max"):
        pytest.skip("ephemeral pool disabled (hitkit up forge --ephemeral 1)")
    put_file(ctx["admin"], ctx["repo"], ".forgejo/workflows/eph.yml",
             "on: [workflow_dispatch]\njobs:\n  eph:\n    runs-on: ephemeral\n    steps:\n"
             "      - run: test \"$(hostname)\" != runner-1 && echo fresh-vm\n")
    run = run_workflow(ctx["admin"], ctx["repo"], "eph.yml", timeout=600)
    assert run["status"] == "success"
    runners = [r for r in ctx["admin"].get("/admin/actions/runners") if r.get("ephemeral")]
    assert not runners, "ephemeral registrations must be gone after the job"


def test_jobs_use_the_pull_through_caches(ctx):
    if not ctx["hctx"].paths("cache").known_hosts.exists():
        pytest.skip("cache kit not up")
    put_file(ctx["admin"], ctx["repo"], ".forgejo/workflows/caches.yml",
             "on: [workflow_dispatch]\njobs:\n  caches:\n    runs-on: ubuntu-latest\n"
             "    services:\n      redis:\n        image: redis:7-alpine\n    steps:\n"
             "      - run: test \"$PIP_INDEX_URL\" = https://cache.hitkit.internal:5002/index/\n"
             "      - run: pip download --no-deps -d /tmp/p six==1.16.0\n"
             "      - run: npm view left-pad version\n"
             "      - run: curl -fsS https://forgejo.hitkit.internal/api/healthz\n")
    assert run_workflow(ctx["admin"], ctx["repo"], "caches.yml")["status"] == "success"


def test_proxmox_api_with_hitkit_token_and_tls():
    from hitkit.cli import _host_context

    if not _host_context().paths("proxmox").known_hosts.exists():
        pytest.skip("proxmox kit not up")
    creds = hitkit("creds", "proxmox")
    req = urllib.request.Request(f"{creds['url']}/api2/json/version",
                                 headers={"Authorization": f"PVEAPIToken={creds['api_token']}"})
    # Verified TLS: the host trusts the (name-constrained) hitkit CA.
    with urllib.request.urlopen(req, timeout=20) as resp:
        assert json.loads(resp.read())["data"]["release"].startswith("9.")
