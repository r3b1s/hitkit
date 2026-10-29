"""Data disks are never deleted implicitly (design §13.13): orphaned directories, destroy, gc."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hitkit.addressing import parse_subnet
from hitkit.errors import HitkitError
from hitkit.machine import creds, disks, lifecycle
from hitkit.machine.images import Image
from hitkit.machine.metadata import MachineMeta, Ownership
from hitkit.machine.spec import DOMAIN_PREFIX, MachineSpec

SUBNET = parse_subnet("10.77.0.0/24")
SPEC = MachineSpec(name="forgejo", role="forgejo", cpus=1, memory_mib=512, data_disk_gib=1, kit="forge", kit_version=1)


class FakeDom:
    def __init__(self, name="forgejo"):
        self._name = DOMAIN_PREFIX + name
        self.active, self.undefined = True, False

    def name(self):
        return self._name

    def isActive(self):
        return self.active

    def create(self):
        self.active = True

    def destroy(self):
        self.active = False

    def undefineFlags(self, flags):
        self.undefined, self.undefine_flags = True, flags


@pytest.fixture
def ctx(user):
    cfg = SimpleNamespace(install_id="mine", network=lambda: SUBNET)
    return lifecycle.HostContext(user=user, cfg=cfg, ca=None)


def machine_dir(ctx, name="forgejo", data="repos"):
    paths = ctx.paths(name)
    paths.root.mkdir(parents=True)
    paths.os_disk.write_text("os")
    paths.data_disk.write_text(data)
    return paths


def kept(ctx, name="forgejo"):
    return ctx.vms_dir / ".kept" / f"{name}-data.qcow2"


@pytest.fixture
def no_domains(monkeypatch):
    monkeypatch.setattr(lifecycle, "get", lambda conn, name, install_id: None)


# --- keep_data_disk -------------------------------------------------------------------


def test_a_second_kept_disk_never_overwrites_the_first(ctx):
    machine_dir(ctx, data="first")
    lifecycle.keep_data_disk(ctx, "forgejo")
    ctx.paths("forgejo").data_disk.write_text("second")

    second = lifecycle.keep_data_disk(ctx, "forgejo")

    assert kept(ctx).read_text() == "first"
    assert second.read_text() == "second" and second.name.startswith("forgejo-data-")
    import os

    os.utime(second, (2_000_000_000, 2_000_000_000))  # the second holds newer data
    listed = lifecycle.kept_disks(ctx)
    assert [(k.path.name == second.name, k.reused_by_up) for k in listed] == [(True, True), (False, False)]
    assert lifecycle.newest_kept_disk(ctx, "forgejo") == second


# --- create over a directory whose domain vanished ---------------------------------------


@pytest.fixture
def create_fakes(monkeypatch, tmp_path):
    state = SimpleNamespace(blank=[], fail_define=False)
    image = Image(role="forgejo", hash="h", path=tmp_path / "img.qcow2", versions={})
    monkeypatch.setattr(lifecycle.images, "ensure", lambda user, role, log=None: image)
    monkeypatch.setattr(disks, "create_overlay", lambda base, path: path.write_text("overlay"))
    monkeypatch.setattr(disks, "create_data_disk", lambda path, gib: (state.blank.append(path), path.write_text("")))
    monkeypatch.setattr(creds, "ensure_client_key", lambda user: "ssh-ed25519 K")
    monkeypatch.setattr(creds, "build", lambda *a, **k: creds.MachineCredentials({}, "known"))
    state.disk_gib, state.resized = 1, []
    monkeypatch.setattr(disks, "info", lambda path: {"virtual-size": state.disk_gib * 2**30, "actual-size": 0})
    monkeypatch.setattr(disks, "resize", lambda path, gib: state.resized.append(gib))

    def define(xml):
        if state.fail_define:
            raise RuntimeError("libvirt said no")
        return FakeDom()

    state.conn = SimpleNamespace(defineXML=define)
    return state


def test_up_after_the_domain_vanished_brings_the_machine_back_with_its_data(ctx, create_fakes):
    # Regression: this path used to rmtree the directory, data disk included.
    machine_dir(ctx, data="repos")
    messages = []

    lifecycle.create(create_fakes.conn, ctx, SPEC, messages.append, {})

    assert ctx.paths("forgejo").data_disk.read_text() == "repos"
    assert create_fakes.blank == []
    assert any("without a libvirt domain" in m for m in messages)


def test_an_orphaned_directory_wins_over_an_older_kept_disk(ctx, create_fakes):
    # Regression: the older kept disk was reused and the orphan's newer data set aside.
    k = kept(ctx)
    k.parent.mkdir(parents=True)
    k.write_text("older")
    machine_dir(ctx, data="newest")

    lifecycle.create(create_fakes.conn, ctx, SPEC, lambda m: None, {})

    assert ctx.paths("forgejo").data_disk.read_text() == "newest"
    assert k.read_text() == "older"  # still kept, untouched


def test_a_reused_disk_smaller_than_the_spec_is_grown(ctx, create_fakes):
    machine_dir(ctx, data="repos")
    create_fakes.disk_gib = 1

    from dataclasses import replace

    lifecycle.create(create_fakes.conn, ctx, replace(SPEC, data_disk_gib=50), lambda m: None, {})

    assert create_fakes.resized == [50]


def test_a_failed_create_returns_a_reused_data_disk_to_kept(ctx, create_fakes):
    machine_dir(ctx, data="repos")
    create_fakes.fail_define = True

    with pytest.raises(RuntimeError):
        lifecycle.create(create_fakes.conn, ctx, SPEC, lambda m: None, {})

    assert kept(ctx).read_text() == "repos"
    assert not ctx.paths("forgejo").root.exists()


def test_job_vms_on_a_template_keep_nothing(ctx, create_fakes, tmp_path):
    machine_dir(ctx, name="ci-1", data="job scratch")
    spec = MachineSpec(name="ci-1", role="runner", cpus=1, memory_mib=512, data_disk_gib=1, kit="forge", kit_version=1)

    lifecycle.create(create_fakes.conn, ctx, spec, lambda m: None, {}, data_backing=tmp_path / "template.qcow2")

    assert lifecycle.kept_disks(ctx) == []


# --- destroy ---------------------------------------------------------------------------------


def test_destroy_keeps_data_by_default_even_without_a_domain(ctx, no_domains):
    machine_dir(ctx)

    assert lifecycle.destroy(None, ctx, "forgejo") is True

    assert kept(ctx).read_text() == "repos"
    assert not ctx.paths("forgejo").root.exists()


def test_destroy_with_delete_data_deletes_it(ctx, no_domains):
    machine_dir(ctx)

    lifecycle.destroy(None, ctx, "forgejo", delete_data=True)

    assert lifecycle.kept_disks(ctx) == []


def test_destroy_of_an_owned_domain_undefines_it_and_keeps_data(ctx, monkeypatch):
    machine_dir(ctx)
    dom = FakeDom()
    info = lifecycle.MachineInfo("forgejo", Ownership.OWNED, None, "running")
    monkeypatch.setattr(lifecycle, "get", lambda conn, name, install_id: (dom, info))
    monkeypatch.setattr(lifecycle, "_libvirt", lambda: SimpleNamespace(
        VIR_DOMAIN_UNDEFINE_NVRAM=4, VIR_DOMAIN_UNDEFINE_MANAGED_SAVE=1, libvirtError=Exception))
    stopped = []
    monkeypatch.setattr(lifecycle, "stop", lambda conn, ctx, name, timeout=60: stopped.append(name) or True)

    lifecycle.destroy(None, ctx, "forgejo")

    assert stopped == ["forgejo"]  # shut down cleanly before the disk is kept
    assert dom.undefined and kept(ctx).read_text() == "repos"


class SavedDom(FakeDom):
    """A domain libvirt saved to disk when the user's session ended."""

    def __init__(self):
        super().__init__()
        self.active, self.saved, self.shutdowns, self.clock_synced = False, True, 0, False

    def hasManagedSaveImage(self, flags):
        return self.saved

    def create(self):
        self.active, self.saved = True, False

    def shutdown(self):
        self.shutdowns += 1
        self.active = False

    def setTime(self, flags=0):
        self.clock_synced = True


def test_stop_resumes_a_saved_domain_and_shuts_it_down_cleanly(ctx, monkeypatch):
    # Disks must never change under a saved memory image (upgrade, offline backup, restore).
    dom = SavedDom()
    info = lifecycle.MachineInfo("forgejo", Ownership.OWNED, None, "stopped")
    monkeypatch.setattr(lifecycle, "get", lambda conn, name, install_id: (dom, info))

    assert lifecycle.stop(None, ctx, "forgejo") is True

    assert dom.shutdowns == 1 and not dom.saved and not dom.active


def test_starting_a_saved_domain_resumes_it_and_checks_readiness_over_ssh(ctx, monkeypatch):
    from hitkit.machine import remote

    dom = SavedDom()
    monkeypatch.setattr(lifecycle, "_libvirt", lambda: SimpleNamespace(VIR_DOMAIN_TIME_SYNC=1))
    checks = []
    monkeypatch.setattr(remote, "run", lambda ctx, name, cmd, stdin=None: checks.append(cmd) or "")
    messages = []

    resumed = lifecycle._power_on(ctx, dom, "forgejo", messages.append)
    lifecycle._wait_ready(ctx, dom, "forgejo", fresh_boot=not resumed)

    assert resumed and dom.clock_synced and "resuming" in messages[0]
    assert checks == ["systemctl is-active --quiet hitkit-ready.service || systemctl restart hitkit-ready.service"]


# --- gc ------------------------------------------------------------------------------------------


def _meta(install_id):
    return MachineMeta(install_id=install_id, kit="forge", kit_version=1, machine="forgejo", role="forgejo",
                       image="forgejo-x", address="10.77.0.10", created="2026-01-01T00:00:00+00:00")


def test_directories_of_another_install_are_not_orphans(ctx, monkeypatch):
    # Regression: after config.toml was recreated (new install ID), gc listed every machine.
    machine_dir(ctx)
    machine_dir(ctx, name="devbox")
    conn = SimpleNamespace(listAllDomains=lambda: [FakeDom("forgejo")])
    monkeypatch.setattr(lifecycle, "list_machines", lambda conn, iid: [
        lifecycle.MachineInfo("forgejo", Ownership.INDETERMINATE, _meta("old-install"), "running")])

    assert [d.name for d in lifecycle.orphan_dirs(conn, ctx)] == ["devbox"]


def test_removing_an_orphan_keeps_its_data_disk(ctx, monkeypatch):
    paths = machine_dir(ctx, name="devbox", data="work")
    monkeypatch.setattr(disks, "backing_chain", lambda disk: [])

    (disk,) = lifecycle.remove_orphan(ctx, paths.root)

    assert disk.read_text() == "work" and not paths.root.exists()


def test_removing_an_orphaned_job_vm_keeps_nothing(ctx, monkeypatch, tmp_path):
    paths = machine_dir(ctx, name="ci-3", data="overlay")
    monkeypatch.setattr(disks, "backing_chain", lambda disk: [tmp_path / "template.qcow2"])

    assert lifecycle.remove_orphan(ctx, paths.root) == []
    assert lifecycle.kept_disks(ctx) == []


# --- machines whose OS disk holds state (proxmox, ludus) -------------------------------------

STATEFUL = MachineSpec(name="ludus", role="ludus", cpus=1, memory_mib=512, data_disk_gib=1, kit="ludus",
                       kit_version=1, stateful_os=True)


def test_destroy_keeps_the_os_disk_of_a_stateful_machine(ctx, no_domains):
    paths = machine_dir(ctx, name="ludus")

    lifecycle.destroy(None, ctx, "ludus", stateful_os=True)

    kinds = sorted(k.kind for k in lifecycle.kept_disks(ctx))
    assert kinds == ["data", "os"] and not paths.root.exists()


def test_a_stateful_machine_comes_back_with_its_own_os_disk(ctx, create_fakes, monkeypatch):
    monkeypatch.setattr(disks, "backing_chain", lambda disk: [])
    machine_dir(ctx, name="ludus")
    ctx.paths("ludus").os_disk.write_text("system state")

    lifecycle.create(create_fakes.conn, ctx, STATEFUL, lambda m: None, {})

    assert ctx.paths("ludus").os_disk.read_text() == "system state"  # not a fresh overlay


def test_upgrade_refuses_a_stateful_machine(ctx):
    with pytest.raises(HitkitError, match="never re-imaged"):
        lifecycle.upgrade(None, ctx, STATEFUL, lambda m: None)


def test_proxmox_and_ludus_are_stateful_and_wait_long_enough():
    from hitkit import kits

    assert kits.load("proxmox").machines[0].stateful_os
    ludus = kits.load("ludus").machines[0]
    assert ludus.stateful_os and ludus.ready_timeout >= 1800


# --- installer machine sidecar durability (plan §4.5, §9) --------------------

INCUS_SPEC = MachineSpec(
    name="incus-os", role="incus-os", cpus=4, memory_mib=8192, data_disk_gib=150,
    kit="incus-os", kit_version=1, stateful_os=True, ready_timeout=600,
)


def test_destroy_keeps_nvram_and_tpm_sidecar_for_installer_machine(ctx, no_domains):
    paths = ctx.paths("incus-os")
    paths.root.mkdir(parents=True)
    paths.data_disk.write_text("zfs pool")
    paths.nvram.write_text("nvram keys")
    paths.tpm_dir.mkdir(parents=True)
    (paths.tpm_dir / "tpm2-00.permall").write_text("sealed keys")

    lifecycle.destroy(None, ctx, "incus-os")

    kept_disk = ctx.vms_dir / ".kept" / "incus-os-data.qcow2"
    sidecar = ctx.vms_dir / ".kept" / "incus-os-data.state"
    assert kept_disk.read_text() == "zfs pool"
    assert sidecar.is_dir()
    assert (sidecar / "nvram.fd").read_text() == "nvram keys"
    assert (sidecar / "tpm" / "tpm2-00.permall").read_text() == "sealed keys"
    assert not paths.root.exists()


def test_installer_machine_comes_back_with_its_own_nvram_and_tpm(ctx, create_fakes):
    kept_disk = ctx.vms_dir / ".kept" / "incus-os-data.qcow2"
    sidecar = ctx.vms_dir / ".kept" / "incus-os-data.state"
    kept_disk.parent.mkdir(parents=True)
    kept_disk.write_text("zfs pool")
    sidecar.mkdir(parents=True)
    (sidecar / "nvram.fd").write_text("nvram keys")
    (sidecar / "tpm").mkdir(parents=True)
    (sidecar / "tpm" / "tpm2-00.permall").write_text("sealed keys")

    create_fakes.disk_gib = 150
    lifecycle.create(create_fakes.conn, ctx, INCUS_SPEC, lambda m: None, {})

    paths = ctx.paths("incus-os")
    assert paths.data_disk.read_text() == "zfs pool"
    assert paths.nvram.read_text() == "nvram keys"
    assert (paths.tpm_dir / "tpm2-00.permall").read_text() == "sealed keys"
    assert not sidecar.exists()


def test_installer_backup_and_restore_copies_sidecar(ctx, monkeypatch, tmp_path):
    from hitkit import backup

    paths = ctx.paths("incus-os")
    paths.root.mkdir(parents=True)
    paths.data_disk.write_text("zfs pool")
    paths.nvram.write_text("nvram keys")
    paths.tpm_dir.mkdir(parents=True)
    (paths.tpm_dir / "tpm2-00.permall").write_text("sealed keys")

    dom = FakeDom("incus-os")
    info = lifecycle.MachineInfo("incus-os", Ownership.OWNED, None, "running")
    monkeypatch.setattr(lifecycle, "kit_machines", lambda conn, iid, kit: [info])
    monkeypatch.setattr(lifecycle, "get", lambda conn, name, iid: (dom, info))
    monkeypatch.setattr(lifecycle, "stop", lambda *a, **k: True)
    monkeypatch.setattr(lifecycle, "start", lambda *a, **k: True)
    monkeypatch.setattr(disks, "compressed_copy", lambda src, dest: dest.write_text(src.read_text()))
    monkeypatch.setattr(disks, "expand_copy", lambda src, dest: dest.write_text(src.read_text()))

    from hitkit.kits import Hooks
    kit = SimpleNamespace(name="incus-os", spec_for=lambda n: INCUS_SPEC)
    hooks = Hooks()

    backup_dir = backup.create(None, ctx, kit, hooks, str(tmp_path / "backups"), keep=7, log=lambda m: None)

    manifest = backup.verify(backup_dir)
    assert "state_file" in manifest["machines"]["incus-os"]
    assert (backup_dir / manifest["machines"]["incus-os"]["state_file"]["name"]).exists()

    # Now restore into a fresh machine directory
    paths.data_disk.write_text("current data to keep")
    paths.nvram.write_text("current nvram")

    backup.restore(None, ctx, kit, hooks, backup_dir, log=lambda m: None)

    assert paths.data_disk.read_text() == "zfs pool"
    assert paths.nvram.read_text() == "nvram keys"
    assert (paths.tpm_dir / "tpm2-00.permall").read_text() == "sealed keys"



def installer_dir(ctx, data="zfs pool"):
    paths = ctx.paths("incus-os")
    paths.root.mkdir(parents=True)
    paths.data_disk.write_text(data)
    paths.nvram.write_text("nvram keys")
    paths.tpm_dir.mkdir()
    (paths.tpm_dir / "tpm2-00.permall").write_text("sealed keys")
    return paths


def test_gc_deleting_a_kept_disk_also_deletes_its_tpm_state(ctx, no_domains):
    installer_dir(ctx)
    lifecycle.destroy(None, ctx, "incus-os")
    [k] = lifecycle.kept_disks(ctx)

    lifecycle.delete_kept_disk(k.path)

    assert list((ctx.vms_dir / ".kept").iterdir()) == []  # the sidecar holds key material


def test_only_machines_with_tpm_state_get_a_sidecar(ctx, no_domains):
    paths = machine_dir(ctx)
    paths.nvram.write_text("every UEFI machine has one")

    lifecycle.destroy(None, ctx, "forgejo")

    assert sorted(p.name for p in (ctx.vms_dir / ".kept").iterdir()) == ["forgejo-data.qcow2"]


def test_destroy_keeping_data_tells_libvirt_to_keep_nvram_and_tpm(ctx, monkeypatch):
    installer_dir(ctx)
    dom = FakeDom("incus-os")
    info = lifecycle.MachineInfo("incus-os", Ownership.OWNED, None, "running")
    monkeypatch.setattr(lifecycle, "get", lambda conn, name, install_id: (dom, info))
    monkeypatch.setattr(lifecycle, "stop", lambda conn, ctx, name, timeout=60: True)
    lv = SimpleNamespace(VIR_DOMAIN_UNDEFINE_NVRAM=4, VIR_DOMAIN_UNDEFINE_KEEP_NVRAM=8,
                         VIR_DOMAIN_UNDEFINE_KEEP_TPM=64, VIR_DOMAIN_UNDEFINE_MANAGED_SAVE=1, libvirtError=Exception)
    monkeypatch.setattr(lifecycle, "_libvirt", lambda: lv)

    lifecycle.destroy(None, ctx, "incus-os")

    assert dom.undefine_flags == 8 | 64 | 1


@pytest.fixture
def installer_fakes(create_fakes, monkeypatch):
    from hitkit.machine import seed as seed_disk

    create_fakes.seeds = []
    monkeypatch.setattr(seed_disk, "create_seed_image", lambda path, sections: path.write_text(str(sections)))

    def seed():
        create_fakes.seeds.append(1)
        return {"install": {"version": "1"}}

    create_fakes.seed = seed
    return create_fakes


def test_a_fresh_installer_machine_gets_install_media_and_its_seed(ctx, installer_fakes):
    lifecycle._create(installer_fakes.conn, ctx, INCUS_SPEC, lambda m: None, {}, seed=installer_fakes.seed)

    paths = ctx.paths("incus-os")
    assert installer_fakes.seeds == [1]
    assert paths.install_disk.read_text() == "overlay"
    assert "install" in paths.seed_img.read_text()
    assert paths.tpm_dir.is_dir()
    assert not paths.os_disk.exists()  # the single disk is the data disk


def test_an_installer_machine_from_a_kept_disk_is_not_reinstalled(ctx, installer_fakes, no_domains):
    installer_dir(ctx)
    lifecycle.destroy(None, ctx, "incus-os")
    installer_fakes.disk_gib = 150

    lifecycle._create(installer_fakes.conn, ctx, INCUS_SPEC, lambda m: None, {}, seed=installer_fakes.seed)

    paths = ctx.paths("incus-os")
    assert installer_fakes.seeds == []
    assert not paths.install_disk.exists() and not paths.seed_img.exists()
    assert (paths.tpm_dir / "tpm2-00.permall").read_text() == "sealed keys"


def test_a_kept_installer_disk_without_tpm_state_warns_about_the_recovery_key(ctx, installer_fakes):
    kept_disk = ctx.vms_dir / ".kept" / "incus-os-data.qcow2"
    kept_disk.parent.mkdir(parents=True)
    kept_disk.write_text("zfs pool")
    installer_fakes.disk_gib = 150
    messages = []

    lifecycle._create(installer_fakes.conn, ctx, INCUS_SPEC, messages.append, {}, seed=installer_fakes.seed)

    assert any("recovery key" in m for m in messages)


def test_installer_machines_need_a_seed_and_probe_from_their_kit(ctx, monkeypatch):
    monkeypatch.setattr(lifecycle, "get", lambda conn, name, install_id: None)
    with pytest.raises(HitkitError, match="needs a seed and a readiness probe"):
        lifecycle.up(None, ctx, INCUS_SPEC, lambda m: None)

    meta = MachineMeta(install_id="mine", kit="incus-os", kit_version=1, machine="incus-os", role="incus-os",
                       image="incus-os-h", address="10.77.0.2", created="now", versions={})
    info = lifecycle.MachineInfo("incus-os", Ownership.OWNED, meta, "stopped")
    monkeypatch.setattr(lifecycle, "get", lambda conn, name, install_id: (FakeDom("incus-os"), info))
    with pytest.raises(HitkitError, match="needs a readiness probe"):
        lifecycle.start(None, ctx, "incus-os", lambda m: None)


def test_upgrade_refuses_an_installer_machine(ctx):
    with pytest.raises(HitkitError, match="updates itself in place"):
        lifecycle.upgrade(None, ctx, INCUS_SPEC, lambda m: None)


def test_backups_of_machines_without_tpm_state_have_no_state_file(ctx, monkeypatch, tmp_path):
    from hitkit import backup
    from hitkit.kits import Hooks

    machine_dir(ctx).nvram.write_text("every UEFI machine has one")
    info = lifecycle.MachineInfo("forgejo", Ownership.OWNED, None, "stopped")
    monkeypatch.setattr(lifecycle, "kit_machines", lambda conn, iid, kit: [info])
    monkeypatch.setattr(lifecycle, "get", lambda conn, name, iid: (FakeDom(), info))
    monkeypatch.setattr(lifecycle, "has_saved_state", lambda dom: False)
    monkeypatch.setattr(lifecycle, "stop", lambda *a, **k: True)
    monkeypatch.setattr(lifecycle, "start", lambda *a, **k: True)
    monkeypatch.setattr(disks, "compressed_copy", lambda src, dest: dest.write_text(src.read_text()))
    kit = SimpleNamespace(name="forge", spec_for=lambda n: SPEC)

    backup_dir = backup.create(None, ctx, kit, Hooks(), str(tmp_path / "backups"), keep=7, log=lambda m: None)

    assert "state_file" not in backup.verify(backup_dir)["machines"]["forgejo"]
