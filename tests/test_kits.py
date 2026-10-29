"""Kit definitions and sizing knobs (design §9)."""

from __future__ import annotations

import pytest

from hitkit import kits
from hitkit.errors import HitkitError

DEVBOX_TOML = """
name = "tiny"
description = "test kit"
version = 3

[[machine]]
name = "devbox"
role = "base"
cpus = 2
memory_mib = 1024
data_disk_gib = 5
"""


def test_all_kits_includes_devbox():
    assert "devbox" in {k.name for k in kits.all_kits()}


def test_load_devbox_has_base_role_machine():
    kit = kits.load("devbox")
    assert [(m.name, m.role) for m in kit.machines] == [("devbox", "base")]


def test_load_unknown_kit_raises_hitkit_error():
    with pytest.raises(HitkitError, match="unknown kit"):
        kits.load("nope")


def test_parse_builds_machines_with_kit_identity():
    kit = kits.parse(DEVBOX_TOML)
    assert kit.name == "tiny"
    assert kit.version == 3
    (machine,) = kit.machines
    assert machine.kit == "tiny"
    assert machine.kit_version == 3
    assert machine.data_disk_gib == 5


def test_parse_rejects_machine_name_that_is_not_an_address_slot():
    with pytest.raises(KeyError):
        kits.parse(DEVBOX_TOML.replace('name = "devbox"', 'name = "not-a-slot"'))


def test_load_incus_os_kit():
    from hitkit.addressing import slot

    kit = kits.load("incus-os")
    assert kit.name == "incus-os"
    (m,) = kit.machines
    assert m.name == "incus-os"
    assert m.role == "incus-os"
    assert m.cpus == 4
    assert m.memory_mib == 8192
    assert m.data_disk_gib == 150
    assert m.stateful_os is True
    assert m.ready_timeout == 600

    s = slot("incus-os")
    assert s.host_octet == 2
    assert s.fqdn() == "incus-os.hitkit.internal"

