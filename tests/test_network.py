import xml.etree.ElementTree as ET

from hitkit.addressing import SLOTS, parse_subnet
from hitkit.host.network import METADATA_NS, inspect, network_xml, with_uuid

SUBNET = "10.77.0.0/24"


def _xml(cidr: str = SUBNET) -> str:
    return network_xml(parse_subnet(cidr))


def test_network_xml_parses():
    assert ET.fromstring(_xml()).tag == "network"


def test_network_name_forward_and_bridge():
    root = ET.fromstring(_xml())
    assert root.findtext("name") == "hitkit"
    assert root.find("forward").get("mode") == "nat"
    assert root.find("bridge").get("name") == "hitkit0"


def test_network_ip_and_dhcp_range():
    root = ET.fromstring(_xml())
    ip = root.find("ip")
    assert ip.get("address") == "10.77.0.1"
    assert ip.get("netmask") == "255.255.255.0"
    rng = root.find("ip/dhcp/range")
    assert (rng.get("start"), rng.get("end")) == ("10.77.0.200", "10.77.0.250")


def test_network_has_one_dns_host_per_slot():
    root = ET.fromstring(_xml())
    hosts = root.findall("dns/host")
    actual = {(h.get("ip"), h.findtext("hostname")) for h in hosts}
    expected = {(f"10.77.0.{s.host_octet}", s.fqdn()) for s in SLOTS}
    assert len(hosts) == len(SLOTS)
    assert actual == expected


def test_inspect_own_network_is_owned_and_matches():
    xml = _xml()
    existing = inspect(xml, active=True, desired_xml=xml)
    assert existing.owned is True
    assert existing.subnet == SUBNET
    assert existing.active is True
    assert existing.matches_desired is True


def test_inspect_without_metadata_is_not_owned():
    xml = _xml()
    root = ET.fromstring(xml)
    root.remove(root.find("metadata"))
    existing = inspect(ET.tostring(root, encoding="unicode"), active=True, desired_xml=xml)
    assert existing.owned is False
    assert existing.subnet is None


def test_libvirt_added_uuid_and_mac_do_not_break_match():
    xml = _xml()
    noisy = xml.replace(
        "</name>",
        "</name><uuid>6f1c2a9e-0000-4000-8000-000000000001</uuid>"
        "<mac address='52:54:00:00:00:01'/>",
        1,
    )
    assert "<uuid>" in noisy
    assert inspect(noisy, active=True, desired_xml=xml).matches_desired is True


def test_changed_subnet_does_not_match_desired():
    existing_xml = _xml("10.77.0.0/24")
    desired_xml = _xml("10.88.0.0/24")
    existing = inspect(existing_xml, active=True, desired_xml=desired_xml)
    assert existing.owned is True
    assert existing.matches_desired is False


def test_with_uuid_pins_uuid_and_keeps_definition():
    xml = network_xml(parse_subnet("10.77.0.0/24"))
    pinned = with_uuid(xml, "6a64edd2-930a-41c6-a1f3-0a5e1e2fddd6")
    root = ET.fromstring(pinned)
    assert [u.text for u in root.findall("uuid")] == ["6a64edd2-930a-41c6-a1f3-0a5e1e2fddd6"]
    assert inspect(pinned, True, xml).matches_desired
    # Replacing an existing UUID leaves exactly one.
    again = ET.fromstring(with_uuid(pinned, "00000000-0000-0000-0000-000000000001"))
    assert [u.text for u in again.findall("uuid")] == ["00000000-0000-0000-0000-000000000001"]
