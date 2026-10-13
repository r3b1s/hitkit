"""Machine lifecycle: upgrade with rollback, outdated detection, kept data disks and kit machine listing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hitkit.addressing import parse_subnet
from hitkit.errors import HitkitError
from hitkit.machine import creds, disks, lifecycle, readiness
from hitkit.machine.images import Image
from hitkit.machine.metadata import MachineMeta, Ownership
from hitkit.machine.spec import MachineSpec

SUBNET = parse_subnet("10.77.0.0/24")
SPEC = MachineSpec(name="devbox", role="base", cpus=1, memory_mib=512, data_disk_gib=1, kit="devbox", kit_version=1)


def log(_message: str) -> None:
    pass


def meta(image: str = "base-old", kit: str = "devbox") -> MachineMeta:
    return MachineMeta(install_id="i", kit=kit, kit_version=1, machine="devbox", role="base",
                       image=image, address="10.77.0.40", created="2026-01-01T00:00:00+00:00")


class FakeDom:
    def __init__(self):
        self.active = True

    def isActive(self):
        return self.active

    def create(self):
        self.active = True

    def destroy(self):
        self.active = False


@pytest.fixture
def ctx(user):
    cfg = SimpleNamespace(install_id="i", network=lambda: SUBNET)
    return lifecycle.HostContext(user=user, cfg=cfg, ca=None)


@pytest.fixture
def machine(ctx):
    paths = ctx.paths("devbox")
    paths.root.mkdir(parents=True)
    paths.os_disk.write_text("old-os")
    paths.nvram.write_text("old-nvram")
    return paths


@pytest.fixture
def fakes(monkeypatch, ctx, tmp_path):
    """Replaces libvirt, image and disk work; the real credential sync runs (it only touches files)."""
    state = SimpleNamespace(
        dom=FakeDom(),
        info=lifecycle.MachineInfo("devbox", Ownership.OWNED, meta("base-old"), "running"),
        image=Image(role="base", hash="new", path=tmp_path / "img.qcow2", versions={"x": "1"}),
        stopped=[], redefined=[], synced=[], waits=0, fail_waits=0,
    )
    real_sync = creds.sync_kit_credentials

    def fake_wait(console_log, timeout, is_running, **kwargs):
        state.waits += 1
        if state.waits <= state.fail_waits:
            raise readiness.NotReady("guest reported failure")

    def fake_redefine(conn, dom, ctx, spec, m):
        state.redefined.append(m)
        return dom

    def sync(creds_dir, extra, user):
        state.synced.append(dict(extra))
        return real_sync(creds_dir, extra, user)

    def overlay(base, path):
        path.write_text("new-os")

    monkeypatch.setattr(lifecycle, "get", lambda conn, name, install_id: (state.dom, state.info))
    monkeypatch.setattr(lifecycle.images, "ensure", lambda user, role, log=None: state.image)
    monkeypatch.setattr(lifecycle, "stop", lambda conn, ctx, name, timeout=60: state.stopped.append(name) or True)
    monkeypatch.setattr(disks, "create_overlay", overlay)
    monkeypatch.setattr(lifecycle, "redefine", fake_redefine)
    monkeypatch.setattr(readiness, "wait", fake_wait)
    monkeypatch.setattr(creds, "sync_kit_credentials", sync)
    return state


def test_upgrade_moves_the_machine_onto_the_new_image(ctx, machine, fakes):
    assert lifecycle.upgrade(None, ctx, SPEC, log, {"hitkit.runner.token": b"t"}) is True

    assert fakes.stopped == ["devbox"]
    assert machine.os_disk.read_text() == "new-os"
    assert fakes.redefined[-1].image == "base-new"
    assert fakes.redefined[-1].versions == {"x": "1"}
    assert fakes.synced == [{"hitkit.runner.token": b"t"}]
    assert fakes.dom.active
    assert not (machine.root / "os.qcow2.prev").exists()
    assert not (machine.root / "nvram.fd.prev").exists()


def test_failed_upgrade_rolls_back_disks_and_redefines_with_the_old_meta(ctx, machine, fakes):
    machine.console_log.write_text("HITKIT-FAILED gate=boot detail=x")
    fakes.fail_waits = 1

    with pytest.raises(readiness.NotReady):
        lifecycle.upgrade(None, ctx, SPEC, log, {})

    assert machine.os_disk.read_text() == "old-os"
    assert machine.nvram.read_text() == "old-nvram"
    assert (machine.root / "console.failed.log").read_text() == "HITKIT-FAILED gate=boot detail=x"
    assert fakes.redefined[-1].image == "base-old"
    assert fakes.waits == 2  # the new image, then the rollback
    assert fakes.dom.active


def test_rollback_restores_kit_credentials_the_upgrade_removed(ctx, machine, fakes):
    creds_dir = machine.creds_dir
    creds_dir.mkdir(parents=True)
    (creds_dir / "hitkit.old-token").write_bytes(b"old")
    fakes.fail_waits = 1

    with pytest.raises(readiness.NotReady):
        lifecycle.upgrade(None, ctx, SPEC, log, {})

    assert (creds_dir / "hitkit.old-token").read_bytes() == b"old"


def test_upgrade_of_a_current_machine_returns_false_without_stopping(ctx, machine, fakes):
    fakes.info = lifecycle.MachineInfo("devbox", Ownership.OWNED, meta("base-new"), "running")

    assert lifecycle.upgrade(None, ctx, SPEC, log) is False
    assert fakes.stopped == []
    assert machine.os_disk.read_text() == "old-os"


def test_upgrade_without_a_machine_raises(monkeypatch, ctx):
    monkeypatch.setattr(lifecycle, "get", lambda conn, name, install_id: None)
    with pytest.raises(HitkitError, match="no machine named"):
        lifecycle.upgrade(None, ctx, SPEC, log)


def test_outdated_compares_the_machine_image_with_the_roles_current_id(monkeypatch, ctx):
    info = lifecycle.MachineInfo("devbox", Ownership.OWNED, meta("base-old"), "running")

    monkeypatch.setattr(lifecycle.images, "image_id", lambda role, user=None: "base-new")
    assert lifecycle.outdated(ctx, info) is True

    monkeypatch.setattr(lifecycle.images, "image_id", lambda role, user=None: "base-old")
    assert lifecycle.outdated(ctx, info) is False


def test_outdated_is_false_without_metadata_or_a_known_role(monkeypatch, ctx):
    foreign = lifecycle.MachineInfo("devbox", Ownership.FOREIGN, None, "running")
    assert lifecycle.outdated(ctx, foreign) is False

    def unknown_role(role, user=None):
        raise HitkitError(f"unknown image role {role!r}")

    monkeypatch.setattr(lifecycle.images, "image_id", unknown_role)
    info = lifecycle.MachineInfo("devbox", Ownership.OWNED, meta("base-old"), "running")
    assert lifecycle.outdated(ctx, info) is False


def test_keep_data_disk_moves_the_disk_aside_for_the_next_create(ctx, machine):
    machine.data_disk.write_text("data")

    assert lifecycle.keep_data_disk(ctx, "devbox") == ctx.vms_dir / ".kept" / "devbox-data.qcow2"

    assert not machine.data_disk.exists()
    assert (ctx.vms_dir / ".kept" / "devbox-data.qcow2").read_text() == "data"


def test_keep_data_disk_without_a_disk_is_a_no_op(ctx, machine):
    assert lifecycle.keep_data_disk(ctx, "devbox") is None


def test_create_reuses_a_kept_data_disk_instead_of_a_blank_one(monkeypatch, ctx, tmp_path):
    kept = ctx.vms_dir / ".kept" / "devbox-data.qcow2"
    kept.parent.mkdir(parents=True)
    kept.write_text("kept-data")
    image = Image(role="base", hash="h", path=tmp_path / "img.qcow2", versions={})
    blank: list = []
    monkeypatch.setattr(lifecycle.images, "ensure", lambda user, role, log=None: image)
    monkeypatch.setattr(disks, "create_overlay", lambda base, path: path.write_text("os"))
    monkeypatch.setattr(disks, "create_data_disk", lambda path, gib: blank.append(path))
    monkeypatch.setattr(disks, "info", lambda path: {"virtual-size": 2**30})
    monkeypatch.setattr(creds, "ensure_client_key", lambda user: "ssh-ed25519 K")
    monkeypatch.setattr(creds, "build", lambda *a, **k: creds.MachineCredentials({}, "known"))
    conn = SimpleNamespace(defineXML=lambda xml: FakeDom())

    lifecycle.create(conn, ctx, SPEC, log, {})

    assert blank == []
    assert ctx.paths("devbox").data_disk.read_text() == "kept-data"
    assert not kept.exists()


def test_kit_machines_returns_only_owned_machines_of_that_kit(monkeypatch, ctx):
    infos = [
        lifecycle.MachineInfo("forgejo", Ownership.OWNED, meta(kit="forge"), "running"),
        lifecycle.MachineInfo("devbox", Ownership.OWNED, meta(kit="devbox"), "running"),
        lifecycle.MachineInfo("stray", Ownership.FOREIGN, None, "running"),
        lifecycle.MachineInfo("other", Ownership.INDETERMINATE, meta(kit="forge"), "running"),
    ]
    monkeypatch.setattr(lifecycle, "list_machines", lambda conn, install_id: infos)

    assert [i.name for i in lifecycle.kit_machines(None, "i", "forge")] == ["forgejo"]
