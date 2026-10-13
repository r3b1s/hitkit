"""Ephemeral runner pool: labels, template keys, the reconcile loop and the user service unit."""

from __future__ import annotations

import dataclasses
import json
import time
from types import SimpleNamespace

import pytest

from hitkit import forgepool
from hitkit.machine.spec import MachinePaths, MachineSpec

SPEC = MachineSpec(name="ci-1", role="runner", cpus=1, memory_mib=512, data_disk_gib=1, kit="forge", kit_version=1)


class FakeAdmin:
    """Records every call; deleting a runner removes it from the listing, as Forgejo does."""

    def __init__(self, jobs=(), runners=()):
        self.jobs, self.runners = list(jobs), list(runners)
        self.calls: list[tuple[str, str, object]] = []
        self._next_id = 100

    def get(self, path):
        self.calls.append(("get", path, None))
        return self.jobs if path.startswith("/admin/actions/runners/jobs") else self.runners

    def post(self, path, body=None):
        self.calls.append(("post", path, body))
        self._next_id += 1
        return {"id": self._next_id, "uuid": "u", "token": "t"}

    def delete(self, path, ok=(204, 404)):
        self.calls.append(("delete", path, None))
        rid = path.rsplit("/", 1)[-1]
        self.runners = [r for r in self.runners if str(r["id"]) != rid]

    def deleted(self) -> list[str]:
        return [path for method, path, _ in self.calls if method == "delete"]

    def posted_bodies(self) -> list[dict]:
        return [body for method, _, body in self.calls if method == "post"]


@pytest.fixture
def hctx(user):
    return SimpleNamespace(
        user=user,
        cfg=SimpleNamespace(install_id="i"),
        paths=lambda name: MachinePaths.for_machine(user.data_dir / "vms", name),
    )


@pytest.fixture
def template(tmp_path):
    path = tmp_path / "runner-data.qcow2"
    path.write_bytes(b"")
    return path


@pytest.fixture
def fakes(monkeypatch, hctx):
    """libvirt-facing lifecycle calls, replaced by recorders."""
    machines: list = []
    created: list = []
    destroyed: list[str] = []

    def create(conn, ctx, spec, log, creds, data_backing=None, disposable=False):
        created.append(SimpleNamespace(name=spec.name, role=spec.role, creds=creds, data_backing=data_backing,
                                       disposable=disposable))
        ctx.paths(spec.name).root.mkdir(parents=True, exist_ok=True)

    def destroy(conn, ctx, name, force=False, delete_data=False):
        assert delete_data, "job VMs hold nothing worth keeping"
        destroyed.append(name)
        return True

    monkeypatch.setattr(forgepool.lifecycle, "kit_machines", lambda conn, install_id, kit: list(machines))
    monkeypatch.setattr(forgepool.lifecycle, "create", create)
    monkeypatch.setattr(forgepool.lifecycle, "destroy", destroy)
    return SimpleNamespace(machines=machines, created=created, destroyed=destroyed)


def vm(name: str, state: str = "running"):
    return SimpleNamespace(name=name, state=state)


def job(n: int, handle: str) -> dict:
    return {"id": n, "handle": handle, "status": "waiting", "runs_on": ["ephemeral"]}


def runner(rid: int, name: str, ephemeral: bool = True) -> dict:
    return {"id": rid, "name": name, "ephemeral": ephemeral}


def write_state(hctx, name: str, **state) -> None:
    root = hctx.paths(name).root
    root.mkdir(parents=True, exist_ok=True)
    (root / "pool.json").write_text(json.dumps(state))


def cfg_for(template, **overrides) -> dict:
    cfg = {"max": 2, "labels": ["ephemeral"], "template": str(template), "spec": dataclasses.asdict(SPEC)}
    return cfg | overrides


def pool_for(hctx, admin, logs=None):
    return forgepool.Pool(hctx, admin, (logs if logs is not None else []).append)


def test_labels_for_adds_ubuntu_labels_only_without_persistent_runners():
    assert forgepool.labels_for(0) == ["ephemeral", "ubuntu-latest", "ubuntu-24.04"]
    assert forgepool.labels_for(2) == ["ephemeral"]


def test_template_path_is_deterministic_and_independent_of_prepull_order(user):
    path = forgepool.template_path(user, ["x", "y"], "img")
    assert path == forgepool.template_path(user, ["x", "y"], "img")
    assert path == forgepool.template_path(user, ["y", "x"], "img")


def test_template_path_changes_with_prepull_list_or_runner_image(user):
    base = forgepool.template_path(user, ["x"], "img")
    assert forgepool.template_path(user, ["x", "z"], "img") != base
    assert forgepool.template_path(user, ["x"], "img2") != base


def test_unit_text_runs_the_pool_and_restarts_always():
    text = forgepool.unit_text()
    assert "ExecStart=" in text and " pool run" in text
    assert "Restart=always" in text


def test_tick_starts_one_vm_per_waiting_job_up_to_max(hctx, template, fakes):
    admin = FakeAdmin(jobs=[job(1, "h1"), job(2, "h2"), job(3, "h3")])

    summary = pool_for(hctx, admin).tick(None, cfg_for(template))

    assert summary["started"] == ["ci-1", "ci-2"]
    assert [c.name for c in fakes.created] == ["ci-1", "ci-2"]
    assert all(body["ephemeral"] is True for body in admin.posted_bodies())
    assert any("labels=ephemeral" in path for _, path, _ in admin.calls)

    first = fakes.created[0]
    assert first.creds == {
        "hitkit.runner.token": b"t",
        "hitkit.runner.uuid": b"u",
        "hitkit.runner.handle": b"h1",
    }
    assert first.data_backing == template
    for name, handle in (("ci-1", "h1"), ("ci-2", "h2")):
        state = json.loads((hctx.paths(name).root / "pool.json").read_text())
        assert state["handle"] == handle
        assert state["runner_id"]


def test_running_job_is_not_started_again_and_still_takes_a_slot(hctx, template, fakes):
    fakes.machines.append(vm("ci-1"))
    write_state(hctx, "ci-1", handle="h1", job_id=1, runner_id=7, started=time.time())
    admin = FakeAdmin(jobs=[job(1, "h1"), job(2, "h2"), job(3, "h3")], runners=[runner(7, "ci-1")])

    summary = pool_for(hctx, admin).tick(None, cfg_for(template))

    assert summary["running"] == ["ci-1"]
    assert summary["started"] == ["ci-2"]  # max 2, one slot taken by ci-1
    assert [c.name for c in fakes.created] == ["ci-2"]
    assert fakes.created[0].creds["hitkit.runner.handle"] == b"h2"
    assert fakes.destroyed == []
    assert admin.deleted() == []  # the live runner (id 7) is kept


def test_stopped_job_vm_is_destroyed_and_its_runner_deleted_by_id(hctx, template, fakes):
    fakes.machines.append(vm("ci-1", state="stopped"))
    write_state(hctx, "ci-1", handle="h1", job_id=1, runner_id=7, started=time.time())
    admin = FakeAdmin(runners=[runner(7, "ci-1")])

    summary = pool_for(hctx, admin).tick(None, cfg_for(template))

    assert fakes.destroyed == ["ci-1"]
    assert admin.deleted() == ["/admin/actions/runners/7"]
    assert summary["running"] == []


def test_job_vm_older_than_timeout_is_reaped(hctx, template, fakes):
    fakes.machines.append(vm("ci-1"))
    write_state(hctx, "ci-1", handle="h1", job_id=1, runner_id=7,
                started=time.time() - forgepool.JOB_TIMEOUT - 1)
    admin = FakeAdmin(runners=[runner(7, "ci-1")])
    logs: list[str] = []

    summary = pool_for(hctx, admin, logs).tick(None, cfg_for(template))

    assert fakes.destroyed == ["ci-1"]
    assert admin.deleted() == ["/admin/actions/runners/7"]
    assert summary["running"] == []
    assert any("timed out" in line for line in logs)


def test_nothing_starts_when_the_template_does_not_exist(hctx, tmp_path, fakes):
    admin = FakeAdmin(jobs=[job(1, "h1")])

    summary = pool_for(hctx, admin).tick(None, cfg_for(tmp_path / "missing.qcow2"))

    assert summary["started"] == []
    assert fakes.created == []
    assert admin.posted_bodies() == []


def test_drain_destroys_every_job_vm_and_leaves_other_machines(hctx, fakes):
    fakes.machines.extend([vm("ci-1"), vm("ci-2", state="stopped"), vm("devbox")])
    write_state(hctx, "ci-1", handle="h1", job_id=1, runner_id=7, started=time.time())
    admin = FakeAdmin(runners=[runner(7, "ci-1")])

    names = pool_for(hctx, admin).drain(None)

    assert names == ["ci-1", "ci-2"]
    assert fakes.destroyed == ["ci-1", "ci-2"]
    assert admin.deleted() == ["/admin/actions/runners/7"]


def test_sweep_deletes_only_unkept_ephemeral_ci_runners(hctx):
    admin = FakeAdmin(runners=[
        runner(1, "ci-1"),                       # kept: its VM is alive
        runner(2, "ci-2"),                       # orphan: no VM
        runner(3, "ci-3", ephemeral=False),      # persistent runner
        runner(4, "runner-1"),                   # not a pool runner
    ])

    pool_for(hctx, admin)._sweep_runners({1})

    assert admin.deleted() == ["/admin/actions/runners/2"]


# --- flavors: several kinds of job VM (the Arch runner next to the default one) ---------------

ARCH = dataclasses.replace(SPEC, role="runner-arch")


def two_flavors(template, **overrides) -> dict:
    return {"max": 3, "flavors": [
        {"labels": ["ephemeral"], "spec": dataclasses.asdict(SPEC), "template": str(template)},
        {"labels": ["arch"], "spec": dataclasses.asdict(ARCH), "template": None},
    ]} | overrides


def test_old_pool_config_is_one_flavor(template):
    (flavor,) = forgepool.flavors(cfg_for(template))
    assert flavor["labels"] == ["ephemeral"] and flavor["template"] == str(template)


def test_each_job_gets_the_flavor_its_runs_on_asks_for(hctx, template, fakes):
    arch_job = {**job(2, "h2"), "runs_on": ["arch"]}
    admin = FakeAdmin(jobs=[job(1, "h1"), arch_job])

    pool_for(hctx, admin).tick(None, two_flavors(template))

    by_name = {c.name: c for c in fakes.created}
    assert by_name["ci-1"].role == "runner" and by_name["ci-1"].data_backing == template
    assert by_name["ci-2"].role == "runner-arch" and by_name["ci-2"].data_backing is None
    assert all(c.disposable for c in fakes.created)  # job VMs never keep their disks
    assert any("labels=arch,ephemeral" in path for _, path, _ in admin.calls)


def test_jobs_no_flavor_serves_are_left_alone(hctx, template, fakes):
    admin = FakeAdmin(jobs=[{**job(1, "h1"), "runs_on": ["windows-latest"]}])

    summary = pool_for(hctx, admin).tick(None, two_flavors(template))

    assert summary["started"] == [] and fakes.created == []


def test_a_flavor_without_template_starts_while_another_template_is_missing(hctx, tmp_path, fakes):
    admin = FakeAdmin(jobs=[job(1, "h1"), {**job(2, "h2"), "runs_on": ["arch"]}])

    summary = pool_for(hctx, admin).tick(None, two_flavors(tmp_path / "not-built-yet.qcow2"))

    assert summary["started"] == ["ci-1"] and fakes.created[0].role == "runner-arch"
