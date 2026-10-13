"""End-to-end checks for the MVP exit criteria (design §11, phases 2–4).

Runs against a live `hitkit up forge` on this host, so it is opt-in:

    HITKIT_E2E=1 uv run pytest tests/e2e -v

Requires the hitkit CA in the host trust store (`sudo hitkit trust install`)
so that git verifies TLS without any special configuration.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HITKIT_E2E") != "1", reason="set HITKIT_E2E=1 to run")

HITKIT = [str(Path(__file__).resolve().parents[2] / ".venv" / "bin" / "hitkit"), "--json"]

WORKFLOW = """\
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4
        with:
          path: ~/.cache/hitkit-e2e
          key: hitkit-e2e-${{ github.sha }}
      - name: Use the checkout and populate the cache
        run: |
          test -f package.json
          git log --oneline -1
          mkdir -p ~/.cache/hitkit-e2e && date > ~/.cache/hitkit-e2e/stamp
      - name: TLS verification stays on
        run: test "$(git config --get http.sslVerify || echo unset)" != false
"""

# An old dependency Renovate will want to update.
PACKAGE_JSON = '{\n  "name": "hitkit-e2e",\n  "version": "1.0.0",\n  "dependencies": {"lodash": "4.17.0"}\n}\n'


def hitkit(*args: str):
    out = subprocess.run(HITKIT + list(args), capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.fixture(scope="module")
def forge():
    from hitkit.forgejo import Auth, Forgejo
    from hitkit.host.ca import ca_cert_path
    from hitkit.paths import current_user

    creds = hitkit("creds", "forge")
    name = f"e2e-{int(time.time())}"
    tok = hitkit("token", "create", "forge", "--name", name,
                 "--scope", "write:repository", "--scope", "write:issue", "--scope", "write:user",
                 "--scope", "read:organization")
    api = Forgejo(ca_cert_path(current_user()), Auth(token=tok["token"]))
    repo = api.post("/user/repos", {"name": name, "default_branch": "main"})
    yield {"api": api, "user": creds["username"], "repo": repo["name"], "token": tok["token"], "creds": creds}
    hitkit("token", "revoke", "forge", name)


def wait_for(fn, timeout: float, interval: float = 5.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (value := fn()) is not None:
            return value
        time.sleep(interval)
    pytest.fail(f"timed out waiting for {what}")


def test_phase2_push_over_https_with_verified_tls(forge, tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    (work / "package.json").write_text(PACKAGE_JSON)
    wf = work / ".forgejo" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(WORKFLOW)

    def git(*args):
        subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True, text=True)

    git("init", "-b", "main")
    git("-c", "user.name=e2e", "-c", "user.email=e2e@hitkit.internal", "add", ".")
    git("-c", "user.name=e2e", "-c", "user.email=e2e@hitkit.internal", "commit", "-m", "e2e")
    url = f"https://forgejo.hitkit.internal/{forge['user']}/{forge['repo']}.git"
    # No sslVerify override, no CA override: the host trust store must suffice.
    result = subprocess.run(
        ["git", "-C", str(work), "-c", f"http.extraHeader=Authorization: token {forge['token']}",
         "push", url, "main"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "certificate" not in result.stderr.lower()


def test_phase3_workflow_with_checkout_and_cache_passes(forge):
    api, path = forge["api"], f"/repos/{forge['user']}/{forge['repo']}"

    def finished_run():
        runs = api.get(f"{path}/actions/runs").get("workflow_runs", [])
        done = [r for r in runs if r.get("status") in ("success", "failure", "cancelled", "skipped")]
        return done[0] if done else None

    # First run pulls the job image; allow for it.
    run = wait_for(finished_run, timeout=1200, what="the CI run to finish")
    if run["status"] != "success":
        logs = api.request("GET", f"{path}/actions/runs/{run['id']}/logs", ok=(200, 404))
        pytest.fail(f"workflow ended {run['status']}: {str(logs)[-2000:]}")


def test_phase4_renovate_opens_a_pull_request(forge):
    api = forge["api"]
    owner, repo = forge["user"], forge["repo"]
    renovate_bot = forge["creds"]["renovate_bot"]
    api.put(f"/repos/{owner}/{repo}/collaborators/{renovate_bot}", {"permission": "write"})
    api.post("/repos/hitkit/renovate/actions/workflows/renovate.yml/dispatches", {"ref": "main"})

    def renovate_pr():
        pulls = api.get(f"/repos/{owner}/{repo}/pulls?state=open")
        return next((p for p in pulls if "lodash" in p["title"].lower()), None)

    pr = wait_for(renovate_pr, timeout=1500, interval=15, what="Renovate to open a lodash PR")
    assert pr["user"]["login"] == renovate_bot


def test_rerunning_up_is_idempotent_and_keeps_ci_working(forge):
    """Regression: re-registration used to wipe runner labels, stalling every job."""
    hitkit("up", "forge")
    from hitkit import kits
    from hitkit.cli import _host_context

    admin = kits.hooks("forge").admin_api(_host_context())
    for runner in admin.get("/admin/actions/runners"):
        assert "ubuntu-latest" in runner["labels"], runner
