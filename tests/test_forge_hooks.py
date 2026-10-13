"""Forge kit hooks: runner identity, human usernames, credentials, Renovate workflow."""

from __future__ import annotations

import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hitkit import kits
from hitkit.errors import HitkitError
from hitkit.machine.creds import MAX_NAME
from hitkit.machine.spec import MachinePaths, MachineSpec

# kits.hooks() execs hooks.py without registering it in sys.modules, so the
# module-level helpers are reached through the globals of a KitHooks method.
MOD = SimpleNamespace(**kits.hooks("forge").credentials.__globals__)

FORGEJO_KEYS = {
    "hitkit.fj.secret-key", "hitkit.fj.internal-token", "hitkit.fj.jwt-secret",
    "hitkit.fj.lfs-jwt-secret", "hitkit.fj.admin-password",
    "hitkit.fj.git-host-key",
}


@pytest.fixture
def hooks():
    return kits.hooks("forge")


@pytest.fixture
def hctx(user):
    return SimpleNamespace(user=user, paths=lambda name: MachinePaths.for_machine(user.data_dir / "vms", name))


def _machine(name: str, role: str) -> MachineSpec:
    return MachineSpec(name=name, role=role, cpus=2, memory_mib=2048, data_disk_gib=20, kit="forge", kit_version=1)


def test_runner_uuid_matches_forgejo_derivation():
    assert MOD.runner_uuid("fedcba9876543210fedcba9876543210fedcba98") == "66656463-6261-3938-3736-353433323130"


@pytest.mark.parametrize(
    "name, expected",
    [("T.Dev", "t.dev"), ("Jo Smith!", "josmith"), ("hitkit-admin", "developer"),
     ("renovate-bot", "developer"), ("hitkit", "developer")],
)
def test_human_username(user, name, expected):
    assert MOD.human_username(SimpleNamespace(user=replace(user, name=name))) == expected


def test_forgejo_credentials_are_complete_stable_and_short_enough(hooks, hctx):
    machine = _machine("forgejo", "forgejo")

    creds = hooks.credentials(hctx, machine, ())

    assert set(creds) == FORGEJO_KEYS
    assert all(len(name) <= MAX_NAME for name in creds)
    assert re.fullmatch(r"[0-9a-f]{64}", creds["hitkit.fj.secret-key"].decode())
    assert hooks.credentials(hctx, machine, ()) == creds


def test_runner_gets_token_and_matching_uuid(hooks, hctx):
    machine = _machine("runner-1", "runner")

    creds = hooks.credentials(hctx, machine, ())

    token = creds["hitkit.runner.token"].decode()
    assert set(creds) == {"hitkit.runner.token", "hitkit.runner.uuid"}
    assert re.fullmatch(r"[0-9a-f]{40}", token)
    assert creds["hitkit.runner.uuid"].decode() == MOD.runner_uuid(token)
    assert hooks.credentials(hctx, machine, ()) == creds


def test_each_runner_gets_a_different_token(hooks, hctx):
    first = hooks.credentials(hctx, _machine("runner-1", "runner"), ())["hitkit.runner.token"]
    second = hooks.credentials(hctx, _machine("runner-2", "runner"), ())["hitkit.runner.token"]
    assert first != second


def test_base_role_machine_gets_no_extra_credentials(hooks, hctx):
    assert hooks.credentials(hctx, _machine("devbox", "base"), ()) == {}


def test_creds_before_bring_up_raises(hooks, hctx):
    with pytest.raises(HitkitError, match="not been brought up"):
        hooks.creds(hctx)


def test_renovate_workflow_is_scheduled_and_wired_to_forgejo():
    wf = MOD.RENOVATE_WORKFLOW
    assert "workflow_dispatch" in wf
    assert "@sha256:" in wf
    assert "RENOVATE_PLATFORM: forgejo" in wf
    assert "${{ secrets.RENOVATE_TOKEN }}" in wf
    assert "${{ github.server_url }}/api/v1" in wf
    assert "http.https://forgejo.hitkit.internal/.sslCAInfo" in wf


def test_runner_registration_keeps_labels_and_sends_secret_on_stdin(user, monkeypatch):
    import types

    from hitkit.machine.spec import MachinePaths, MachineSpec

    h = kits.hooks("forge")
    g = h.after_ready.__globals__
    calls = []
    monkeypatch.setattr(g["remote"], "run", lambda hctx, machine, cmd, stdin=None: calls.append((machine, cmd, stdin)))
    monkeypatch.setattr(type(h), "admin_api", lambda self, hctx: types.SimpleNamespace())
    monkeypatch.setattr(type(h), "_ensure_user", lambda self, *a, **k: "pw")
    monkeypatch.setattr(type(h), "_ensure_git_ssh", lambda self, *a, **k: None)
    hctx = types.SimpleNamespace(user=user)
    plan = kits.load("forge").expand({"runners": 2, "renovate": False})
    forgejo = next(m for m in plan if m.role == "forgejo")
    h.after_ready(hctx, forgejo, plan, {}, lambda m: None)
    assert [c[0] for c in calls] == ["forgejo", "forgejo"]
    for machine, cmd, stdin in calls:
        # Re-registering must not wipe the labels the runner declared (it stalls CI).
        assert "--keep-labels" in cmd and "--secret-stdin -" in cmd
        assert stdin and stdin not in cmd  # the secret never appears in argv


def test_runner_gets_cache_credential_only_when_cache_machine_exists(hooks, hctx):
    machine = _machine("runner-1", "runner")
    assert "hitkit.runner.cache" not in hooks.credentials(hctx, machine, ())
    kh = hctx.paths("cache").known_hosts
    kh.parent.mkdir(parents=True)
    kh.write_text("x")
    assert hooks.credentials(hctx, machine, ())["hitkit.runner.cache"] == b"https://cache.hitkit.internal"


def test_job_vms_are_sized_like_runners(monkeypatch, user):
    # Regression: the pool used the runner's default size, ignoring --cpus/--memory runner=N.
    import contextlib
    from types import SimpleNamespace

    from hitkit import forgepool, kits, secrets, virt

    secrets.put(user, "forge", "sizing.json", '{"cpus": {"runner": 6}, "memory_mib": {"*": 4096}}')
    saved = {}
    monkeypatch.setattr(virt, "connect", lambda uri: contextlib.nullcontext(None))
    monkeypatch.setattr(forgepool, "ensure_template", lambda conn, hctx, base, prepull, log: "/t.qcow2")
    monkeypatch.setattr(forgepool, "save_config", lambda u, cfg: saved.update(cfg))
    monkeypatch.setattr(forgepool, "install_service", lambda u: None)

    kits.hooks("forge")._configure_pool(SimpleNamespace(user=user), {"ephemeral": 1, "runners": 1}, lambda m: None)

    spec = saved["flavors"][0]["spec"]
    assert spec["cpus"] == 6 and spec["memory_mib"] == 4096
    assert spec["name"] == "ci-1"


def test_arch_adds_an_arch_job_vm_flavor_to_the_pool(monkeypatch, user):
    import contextlib
    from types import SimpleNamespace

    from hitkit import forgepool, kits, virt
    from hitkit.machine import images

    saved, built = {}, []
    monkeypatch.setattr(virt, "connect", lambda uri: contextlib.nullcontext(None))
    monkeypatch.setattr(forgepool, "ensure_template", lambda conn, hctx, base, prepull, log: "/t.qcow2")
    monkeypatch.setattr(forgepool, "save_config", lambda u, cfg: saved.update(cfg))
    monkeypatch.setattr(forgepool, "install_service", lambda u: None)
    monkeypatch.setattr(images, "ensure", lambda u, role, log: built.append(role))

    kits.hooks("forge")._configure_pool(SimpleNamespace(user=user), {"ephemeral": 2, "runners": 1, "arch": True},
                                        lambda m: None)

    default, arch = saved["flavors"]
    assert default["labels"] == ["ephemeral"] and default["template"] == "/t.qcow2"
    assert arch["labels"] == ["arch"] and arch["template"] is None and arch["spec"]["role"] == "runner-arch"
    assert built == ["runner-arch"]


def test_arch_without_the_pool_is_refused(user):
    from types import SimpleNamespace

    import pytest

    from hitkit import kits
    from hitkit.errors import HitkitError

    with pytest.raises(HitkitError, match="ephemeral pool"):
        kits.hooks("forge")._configure_pool(SimpleNamespace(user=user), {"ephemeral": 0, "arch": True}, lambda m: None)
