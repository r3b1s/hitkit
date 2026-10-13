"""The `hitkit` libvirt network (design §7.1–7.2).

Defined once in ``qemu:///system`` by `hitkit setup`; session-mode VMs attach to
its bridge through qemu-bridge-helper. The XML carries DNS records for every
address slot, so no VM ever needs root to become resolvable.
"""

from __future__ import annotations

import ipaddress
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from hitkit.addressing import DOMAIN, address, dhcp_range, host_records

NETWORK_NAME = "hitkit"
BRIDGE_NAME = "hitkit0"
METADATA_NS = "urn:hitkit:metadata:1"


def network_xml(subnet: ipaddress.IPv4Network) -> str:
    net = ET.Element("network")
    ET.SubElement(net, "name").text = NETWORK_NAME

    meta = ET.SubElement(net, "metadata")
    owner = ET.SubElement(meta, f"{{{METADATA_NS}}}network")
    ET.SubElement(owner, f"{{{METADATA_NS}}}subnet").text = str(subnet)

    ET.SubElement(net, "forward", mode="nat")
    ET.SubElement(net, "bridge", name=BRIDGE_NAME, stp="on", delay="0")
    ET.SubElement(net, "domain", name=DOMAIN, localOnly="yes")

    dns = ET.SubElement(net, "dns")
    for ip, fqdn in host_records(subnet):
        host = ET.SubElement(dns, "host", ip=str(ip))
        ET.SubElement(host, "hostname").text = fqdn

    ip_el = ET.SubElement(net, "ip", address=str(address(subnet, 1)), netmask=str(subnet.netmask))
    dhcp = ET.SubElement(ip_el, "dhcp")
    first, last = dhcp_range(subnet)
    ET.SubElement(dhcp, "range", start=str(first), end=str(last))

    ET.register_namespace("hitkit", METADATA_NS)
    ET.indent(net)
    return ET.tostring(net, encoding="unicode")


def with_uuid(xml: str, uuid: str) -> str:
    """Pin ``uuid`` in a network definition. libvirt rejects redefining a network
    under the same name with a different (or missing) UUID."""
    root = ET.fromstring(xml)
    for old in root.findall("uuid"):
        root.remove(old)
    el = ET.Element("uuid")
    el.text = uuid
    root.insert(1, el)  # right after <name>
    ET.indent(root)
    return ET.tostring(root, encoding="unicode")


def dns_hosts(xml: str) -> dict[str, tuple[str, ...]]:
    """ip -> hostnames for every <dns><host> record."""
    root = ET.fromstring(xml)
    return {h.get("ip"): tuple(sorted(n.text or "" for n in h.findall("hostname")))
            for h in root.findall("dns/host")}


def dns_host_xml(ip: str, hostnames: tuple[str, ...]) -> str:
    host = ET.Element("host", ip=ip)
    for name in hostnames:
        ET.SubElement(host, "hostname").text = name
    return ET.tostring(host, encoding="unicode")


def dns_changes(existing_xml: str, desired_xml: str) -> tuple[list[str], list[str]]:
    """(records to delete, records to add) as <host> XML, to turn existing DNS into desired."""
    have, want = dns_hosts(existing_xml), dns_hosts(desired_xml)
    delete = [dns_host_xml(ip, names) for ip, names in have.items() if want.get(ip) != names]
    add = [dns_host_xml(ip, names) for ip, names in want.items() if have.get(ip) != names]
    return delete, add


@dataclass(frozen=True)
class ExistingNetwork:
    """What setup needs to know about a pre-existing network named `hitkit`."""

    owned: bool  # carries hitkit metadata
    subnet: str | None
    active: bool
    matches_desired: bool  # DNS records, bridge and range all equal what we'd write
    dns_only: bool = False  # differs from desired in DNS records only (updatable live)


def inspect(xml: str, active: bool, desired_xml: str) -> ExistingNetwork:
    root = ET.fromstring(xml)
    subnet_el = root.find(f"metadata/{{{METADATA_NS}}}network/{{{METADATA_NS}}}subnet")
    return ExistingNetwork(
        owned=subnet_el is not None,
        subnet=subnet_el.text if subnet_el is not None else None,
        active=active,
        matches_desired=_fingerprint(root) == _fingerprint(ET.fromstring(desired_xml)),
        dns_only=_fingerprint(root)[:-1] == _fingerprint(ET.fromstring(desired_xml))[:-1],
    )


def _fingerprint(root: ET.Element) -> tuple:
    """The parts of the network definition that matter, ignoring libvirt-added fields (uuid, mac)."""
    dns = sorted(
        (h.get("ip"), tuple(sorted(n.text or "" for n in h.findall("hostname"))))
        for h in root.findall("dns/host")
    )
    ip = root.find("ip")
    rng = root.find("ip/dhcp/range")
    bridge = root.find("bridge")
    fwd = root.find("forward")
    return (
        fwd.get("mode") if fwd is not None else None,
        bridge.get("name") if bridge is not None else None,
        (ip.get("address"), ip.get("netmask")) if ip is not None else None,
        (rng.get("start"), rng.get("end")) if rng is not None else None,
        tuple(dns),
    )
