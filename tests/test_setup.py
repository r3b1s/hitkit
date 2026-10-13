import xml.etree.ElementTree as ET

import pytest

from hitkit.config import HostConfig
from hitkit.errors import Conflict, NeedsRoot
from hitkit.host import managed_files as mf
from hitkit.host import setup
from hitkit.host.network import ExistingNetwork
from hitkit.host.setup import NetworkAction, decide_network_action

SUBNET = "10.77.0.0/24"


def net(owned=True, subnet=SUBNET, active=True, matches=True):
    return ExistingNetwork(owned=owned, subnet=subnet, active=active, matches_desired=matches)


@pytest.mark.parametrize(
    ("existing", "force", "expected"),
    [
        (None, False, NetworkAction.CREATE),
        (net(), False, NetworkAction.KEEP),
        (net(active=False), False, NetworkAction.KEEP),
        (net(matches=False), False, NetworkAction.REPLACE),
        (net(owned=False, subnet=None), False, NetworkAction.REFUSE),
        (net(owned=False, subnet=None), True, NetworkAction.REPLACE),
        (net(subnet="10.88.0.0/24"), False, NetworkAction.REFUSE),
        (net(subnet="10.88.0.0/24"), True, NetworkAction.REPLACE),
    ],
)
def test_decide_network_action(existing, force, expected):
    action, reason = decide_network_action(existing, SUBNET, force)
    assert action is expected
    assert reason


def test_replace_of_active_network_warns_about_running_vms():
    _, reason = decide_network_action(net(matches=False, active=True), SUBNET, False)
    assert "restart" in reason


@pytest.fixture
def system_files(tmp_path, monkeypatch):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    bridge = tmp_path / "bridge.conf"
    bridge.write_text("allow virbr0\n")
    monkeypatch.setattr(mf, "HOSTS_PATH", hosts)
    monkeypatch.setattr(mf, "BRIDGE_CONF_PATH", bridge)
    monkeypatch.setattr(setup.firewall, "ufw_active", lambda: False)
    monkeypatch.setattr(setup.firewall, "firewalld_active", lambda: False)
    monkeypatch.setattr(setup, "_plan_nftables", lambda remove: [])
    monkeypatch.setattr(setup, "_plan_bridge_helper", lambda: [])
    return hosts, bridge


def test_plan_setup_fresh_host_then_idempotent(user, system_files, monkeypatch):
    hosts, bridge = system_files
    monkeypatch.setattr(setup, "_existing_network", lambda xml: None)
    applied_network = []
    monkeypatch.setattr(setup, "_define_and_start", lambda xml, replace: applied_network.append(replace))
    cfg = HostConfig(subnet=SUBNET, install_id="a" * 32)

    changes = setup.plan_setup(user, cfg, trust_ca=False, force=False)
    assert len(changes) == 3
    for c in changes:
        c.apply()
    assert applied_network == [False]
    assert "allow hitkit0" in bridge.read_text() and "allow virbr0" in bridge.read_text()
    assert "forgejo.hitkit.internal" in hosts.read_text()

    monkeypatch.setattr(setup, "_existing_network", lambda xml: net())
    assert setup.plan_setup(user, cfg, trust_ca=False, force=False) == []


def test_plan_setup_refuses_foreign_network(user, system_files, monkeypatch):
    monkeypatch.setattr(setup, "_existing_network", lambda xml: net(owned=False, subnet=None))
    with pytest.raises(Conflict):
        setup.plan_setup(user, HostConfig(SUBNET, "a" * 32), trust_ca=False, force=False)


def test_require_root(monkeypatch):
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)
    with pytest.raises(NeedsRoot):
        setup.require_root()


def test_plan_setup_adds_missing_ufw_rules_as_root(user, system_files, monkeypatch):
    monkeypatch.setattr(setup, "_existing_network", lambda xml: net())
    monkeypatch.setattr(setup.firewall, "ufw_active", lambda: True)
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup.firewall, "show_added", lambda: "")
    added = []
    monkeypatch.setattr(setup.firewall, "add", added.append)
    changes = [c for c in setup.plan_setup(user, HostConfig(SUBNET, "a" * 32), trust_ca=False, force=False)
               if "ufw" in c.summary]
    assert len(changes) == 1
    changes[0].apply()
    assert added == list(setup.firewall.RULES)


def test_dns_only_change_is_updated_in_place():
    action, reason = decide_network_action(
        ExistingNetwork(owned=True, subnet=SUBNET, active=True, matches_desired=False, dns_only=True), SUBNET, False)
    assert action is NetworkAction.UPDATE_DNS and "keep running" in reason


def test_dns_changes_add_and_delete_records():
    from hitkit.addressing import parse_subnet
    from hitkit.host import network

    desired = network.network_xml(parse_subnet(SUBNET))
    root = ET.fromstring(desired)
    dns = root.find("dns")
    dns.remove(dns.findall("host")[-1])  # an older hitkit without the newest slot
    stale = ET.SubElement(dns, "host", ip="10.77.0.9")
    ET.SubElement(stale, "hostname").text = "gone.hitkit.internal"
    existing = ET.tostring(root, encoding="unicode")
    delete, add = network.dns_changes(existing, desired)
    assert len(delete) == 1 and "gone.hitkit.internal" in delete[0]
    assert len(add) == 1
    assert network.inspect(existing, True, desired).dns_only
