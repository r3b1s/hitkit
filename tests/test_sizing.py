"""--cpus / --memory / --disk: N or MACHINE=N, remembered between `up`s (design §13.13)."""

from __future__ import annotations

import pytest

from hitkit import kits
from hitkit.errors import HitkitError


@pytest.fixture
def forge():
    return kits.load("forge")


def plan(kit, sizing, runners=2):
    return {m.name: m for m in kits.apply_sizing(kit, kit.expand(kit.parse_knobs(["--runners", str(runners)])), sizing)}


def test_a_bare_value_sizes_every_machine(forge):
    sizing = kits.parse_sizing(forge, {"data_disk_gib": ("200",)})

    assert {m.data_disk_gib for m in plan(forge, sizing).values()} == {200}


def test_machine_values_target_one_machine_or_every_scaled_instance(forge):
    sizing = kits.parse_sizing(forge, {"data_disk_gib": ("runner=120",), "memory_mib": ("forgejo=4096",)})
    machines = plan(forge, sizing)

    assert machines["runner-1"].data_disk_gib == machines["runner-2"].data_disk_gib == 120
    assert machines["forgejo"].data_disk_gib == 20  # kit default untouched
    assert machines["forgejo"].memory_mib == 4096 and machines["runner-1"].memory_mib == 8192


def test_the_most_specific_value_wins(forge):
    sizing = kits.parse_sizing(forge, {"cpus": ("2",)})
    sizing = kits.parse_sizing(forge, {"cpus": ("runner=6",)}, sizing)

    machines = plan(forge, sizing)
    assert machines["runner-1"].cpus == 6 and machines["forgejo"].cpus == 2


def test_a_bare_value_replaces_earlier_per_machine_values(forge):
    sizing = kits.parse_sizing(forge, {"cpus": ("runner=6",)})

    assert kits.parse_sizing(forge, {"cpus": ("3",)}, sizing) == {"cpus": {"*": 3}}


def test_sizing_is_remembered_when_no_flags_are_given(forge):
    sizing = kits.parse_sizing(forge, {"data_disk_gib": ("runner=120",)})

    assert kits.parse_sizing(forge, {}, sizing) == sizing


@pytest.mark.parametrize("dim,raw,message", [
    ("data_disk_gib", ("nope=10",), "no machine 'nope'"),
    ("data_disk_gib", ("big",), "N or MACHINE=N"),
    ("cpus", ("0",), "between 1 and 64"),
    ("memory_mib", ("forgejo=100",), "between 512"),
])
def test_bad_sizing_is_rejected(forge, dim, raw, message):
    with pytest.raises(HitkitError, match=message):
        kits.parse_sizing(forge, {dim: raw})


# --- existing machines ---------------------------------------------------------------

from dataclasses import replace  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from hitkit.addressing import parse_subnet  # noqa: E402
from hitkit.machine import disks, lifecycle  # noqa: E402
from hitkit.machine.metadata import Ownership  # noqa: E402
from hitkit.machine.spec import MachineSpec  # noqa: E402

SPEC = MachineSpec(name="proxmox", role="proxmox", cpus=4, memory_mib=8192, data_disk_gib=100, kit="proxmox", kit_version=1)


class Dom:
    def __init__(self, active=True):
        self.active, self.resized = active, []

    def isActive(self):
        return self.active

    def blockResize(self, dev, size, flags):
        self.resized.append((dev, size))


@pytest.fixture
def machine(user, monkeypatch):
    ctx = lifecycle.HostContext(user=user, cfg=SimpleNamespace(install_id="i", network=lambda: parse_subnet("10.77.0.0/24")), ca=None)
    paths = ctx.paths("proxmox")
    paths.root.mkdir(parents=True)
    paths.data_disk.write_text("")
    state = SimpleNamespace(ctx=ctx, gib=100, offline=[], redefined=[], grown_fs=[])
    monkeypatch.setattr(disks, "info", lambda path: {"virtual-size": state.gib * 2**30, "actual-size": 0})
    monkeypatch.setattr(disks, "resize", lambda path, gib: state.offline.append(gib))
    monkeypatch.setattr(lifecycle, "redefine", lambda conn, dom, ctx, spec, meta: state.redefined.append(spec) or dom)
    monkeypatch.setattr(lifecycle, "_libvirt", lambda: SimpleNamespace(VIR_DOMAIN_BLOCK_RESIZE_BYTES=1))
    return state


def info(cpus=4, memory=8192):
    return lifecycle.MachineInfo("proxmox", Ownership.OWNED, None, "running", cpus, memory)


def test_a_larger_disk_grows_live_on_a_running_machine(machine):
    dom = Dom(active=True)

    _, grown = lifecycle._reconcile_size(None, dom, machine.ctx, replace(SPEC, data_disk_gib=500), info(), lambda m: None)

    assert grown and dom.resized == [("vdb", 500 * 2**30)] and machine.offline == []


def test_a_larger_disk_grows_offline_on_a_stopped_machine(machine):
    lifecycle._reconcile_size(None, Dom(active=False), machine.ctx, replace(SPEC, data_disk_gib=500), info(), lambda m: None)

    assert machine.offline == [500]


def test_shrinking_a_data_disk_is_refused(machine):
    with pytest.raises(HitkitError, match="refusing to shrink"):
        lifecycle.check_resize(machine.ctx, replace(SPEC, data_disk_gib=50))


def test_cpu_and_memory_changes_redefine_and_say_when_they_apply(machine):
    messages = []

    lifecycle._reconcile_size(None, Dom(active=True), machine.ctx, replace(SPEC, cpus=8), info(), messages.append)

    assert machine.redefined[0].cpus == 8
    assert "next boot" in messages[0]


def test_an_unchanged_machine_is_left_alone(machine):
    dom = Dom()

    _, grown = lifecycle._reconcile_size(None, dom, machine.ctx, SPEC, info(), lambda m: None)

    assert not grown and machine.redefined == [] and dom.resized == []
