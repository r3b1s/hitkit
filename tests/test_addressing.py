import ipaddress

import pytest

from hitkit.addressing import (
    DHCP_FIRST,
    DHCP_LAST,
    SLOTS,
    host_records,
    parse_subnet,
    slot_address,
)

DEFAULT = "10.77.0.0/24"


def test_parse_subnet_accepts_private_24():
    assert parse_subnet(DEFAULT) == ipaddress.IPv4Network(DEFAULT)


def test_parse_subnet_rejects_non_24():
    with pytest.raises(ValueError, match="/24"):
        parse_subnet("10.77.0.0/16")


def test_parse_subnet_rejects_public_range():
    with pytest.raises(ValueError, match="private"):
        parse_subnet("8.8.8.0/24")


def test_parse_subnet_rejects_host_bits():
    with pytest.raises(ValueError):
        parse_subnet("10.77.0.5/24")


def test_slot_names_are_unique():
    names = [s.name for s in SLOTS]
    assert len(names) == len(set(names))


def test_slot_octets_are_unique():
    octets = [s.host_octet for s in SLOTS]
    assert len(octets) == len(set(octets))


def test_no_slot_falls_inside_dhcp_range():
    for s in SLOTS:
        assert not (DHCP_FIRST <= s.host_octet <= DHCP_LAST), s.name


@pytest.mark.parametrize(
    ("name", "octet"),
    [("forgejo", 10), ("runner-1", 20), ("runner-10", 29), ("ci-1", 100), ("ci-100", 199)],
)
def test_slot_address(name, octet):
    net = parse_subnet(DEFAULT)
    assert slot_address(net, name) == ipaddress.IPv4Address(f"10.77.0.{octet}")


def test_host_record_fqdns_end_with_domain():
    records = host_records(parse_subnet(DEFAULT))
    assert len(records) == len(SLOTS)
    assert all(fqdn.endswith(".hitkit.internal") for _, fqdn in records)
