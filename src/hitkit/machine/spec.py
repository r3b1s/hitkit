"""What a machine is, and where its files live (design §8.2)."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path

from hitkit.addressing import slot

DOMAIN_PREFIX = "hitkit-"


@dataclass(frozen=True)
class MachineSpec:
    name: str  # also the address slot name
    role: str  # image role under hitkit/images/
    cpus: int
    memory_mib: int
    data_disk_gib: int
    kit: str
    kit_version: int
    # The OS disk holds state too (Proxmox's config database, Ludus's users and ranges):
    # never replaced by `upgrade`, kept by `destroy`, included in backups.
    stateful_os: bool = False
    ready_timeout: int = 300  # seconds `up` waits for a freshly booted machine to report ready

    def __post_init__(self) -> None:
        slot(self.name)  # every machine must own a slot in the address plan

    @property
    def domain_name(self) -> str:
        return f"{DOMAIN_PREFIX}{self.name}"


def mac_address(subnet: ipaddress.IPv4Network, host_octet: int) -> str:
    """Deterministic MAC in QEMU's 52:54:00 range: 0x68 ('h') + subnet's third octet + host octet."""
    third = subnet.network_address.packed[2]
    return f"52:54:00:68:{third:02x}:{host_octet:02x}"


@dataclass(frozen=True)
class MachinePaths:
    root: Path

    @classmethod
    def for_machine(cls, vms_dir: Path, name: str) -> "MachinePaths":
        return cls(vms_dir / name)

    @property
    def os_disk(self) -> Path:
        return self.root / "os.qcow2"

    @property
    def data_disk(self) -> Path:
        return self.root / "data.qcow2"

    @property
    def nvram(self) -> Path:
        return self.root / "nvram.fd"

    @property
    def creds_dir(self) -> Path:
        return self.root / "creds"

    @property
    def known_hosts(self) -> Path:
        return self.root / "known_hosts"

    @property
    def console_log(self) -> Path:
        return self.root / "console.log"
