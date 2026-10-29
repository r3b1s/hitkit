"""The fixed address plan for the hitkit network (design §7.2).

Topology never changes at runtime (the knob rule), so every machine slot hitkit
will ever use is known up front. That is what lets `hitkit setup` write all DNS
records once, with root, and never need root again.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

DOMAIN = "hitkit.internal"
DEFAULT_SUBNET = "10.77.0.0/24"

# The /24, by last octet:
#   .1–.29     hitkit's own: the host, then one slot per kit machine
#   .30–.99    static addresses for your own guests (nothing in hitkit touches them)
#   .100–.199  DHCP pool for ad-hoc guests
#   .200–.254  overflow for hitkit's own: the ephemeral job VMs (ci-N)
GUEST_STATIC_FIRST = 30
GUEST_STATIC_LAST = 99
DHCP_FIRST = 100
DHCP_LAST = 199
CI_POOL_SIZE = 55  # ci-1 .. ci-55 at .200–.254


@dataclass(frozen=True)
class Slot:
    name: str
    host_octet: int

    def fqdn(self) -> str:
        return f"{self.name}.{DOMAIN}"


def _slots() -> tuple[Slot, ...]:
    slots = [Slot("host", 1), Slot("incus-os", 2), Slot("forgejo", 10)]
    slots += [Slot(f"runner-{i}", 19 + i) for i in range(1, 11)]  # .20–.29
    slots += [Slot("proxmox", 11), Slot("pbs", 12), Slot("ludus", 13), Slot("devbox", 14), Slot("dev", 15)]
    slots += [Slot("dev-debian", 16), Slot("dev-fedora", 17), Slot("cache", 18), Slot("netbird", 19)]
    slots += [Slot(f"ci-{i}", 199 + i) for i in range(1, CI_POOL_SIZE + 1)]  # .200–.254
    return tuple(slots)


SLOTS: tuple[Slot, ...] = _slots()
_BY_NAME = {s.name: s for s in SLOTS}


def parse_subnet(cidr: str) -> ipaddress.IPv4Network:
    """Parse and validate a hitkit subnet. Only private IPv4 /24s are supported."""
    net = ipaddress.IPv4Network(cidr, strict=True)
    if net.prefixlen != 24:
        raise ValueError(f"hitkit subnet must be a /24, got /{net.prefixlen}")
    if not net.is_private:
        raise ValueError(f"hitkit subnet must be a private range, got {net}")
    return net


def address(subnet: ipaddress.IPv4Network, octet: int) -> ipaddress.IPv4Address:
    return subnet.network_address + octet


def slot(name: str) -> Slot:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(f"no address slot named {name!r}") from None


def slot_address(subnet: ipaddress.IPv4Network, name: str) -> ipaddress.IPv4Address:
    return address(subnet, slot(name).host_octet)


def host_records(subnet: ipaddress.IPv4Network) -> list[tuple[ipaddress.IPv4Address, str]]:
    """(address, fqdn) for every slot, in plan order."""
    return [(address(subnet, s.host_octet), s.fqdn()) for s in SLOTS]


def dhcp_range(subnet: ipaddress.IPv4Network) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]:
    return address(subnet, DHCP_FIRST), address(subnet, DHCP_LAST)
