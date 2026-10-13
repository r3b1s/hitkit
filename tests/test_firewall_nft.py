"""Generic nftables / iptables-nft support: rulesets in `nft -j list ruleset` form."""

from hitkit.host import firewall, setup

CUSTOM = {"nftables": [
    {"metainfo": {"version": "1.1"}},
    {"table": {"family": "inet", "name": "filter", "handle": 1}},
    {"chain": {"family": "inet", "table": "filter", "name": "input", "handle": 1,
               "type": "filter", "hook": "input", "prio": 0, "policy": "drop"}},
    {"chain": {"family": "inet", "table": "filter", "name": "forward", "handle": 2,
               "type": "filter", "hook": "forward", "prio": 0, "policy": "drop"}},
    {"chain": {"family": "inet", "table": "filter", "name": "output", "handle": 3,
               "type": "filter", "hook": "output", "prio": 0, "policy": "accept"}},
    # libvirt's own table and docker's iptables-nft FORWARD chain
    {"chain": {"family": "ip", "table": "libvirt_network", "name": "forward", "handle": 1,
               "type": "filter", "hook": "forward", "prio": 0, "policy": "drop"}},
    {"chain": {"family": "ip", "table": "filter", "name": "FORWARD", "handle": 2,
               "type": "filter", "hook": "forward", "prio": 0, "policy": "drop"}},
]}


def chains(ufw=False, firewalld=False):
    return {(c.family, c.table, c.name, c.iptables) for c in firewall.dropping_chains(CUSTOM, ufw, firewalld)}


def test_finds_custom_and_docker_drop_chains_but_not_libvirt_or_accepting_ones():
    assert chains() == {
        ("inet", "filter", "input", False),
        ("inet", "filter", "forward", False),
        ("ip", "filter", "FORWARD", True),
    }


def test_ufw_owns_the_iptables_filter_table_when_active():
    assert ("ip", "filter", "FORWARD", True) not in chains(ufw=True)


def test_firewalld_table_is_left_to_firewalld():
    rs = {"nftables": [{"chain": {"family": "inet", "table": "firewalld", "name": "filter_INPUT",
                                  "hook": "input", "policy": "drop"}}]}
    assert firewall.dropping_chains(rs, ufw=False, firewalld=True) == []
    assert len(firewall.dropping_chains(rs, ufw=False, firewalld=False)) == 1


def test_nft_commands_are_narrow_and_tagged():
    input_chain = firewall.Chain("inet", "filter", "input", "input", False)
    cmds = [add for _check, add in firewall.commands_for(input_chain)]
    assert all(c[:3] == ["nft", "insert", "rule"] for c in cmds)
    joined = [" ".join(c) for c in cmds]
    assert all('iifname hitkit0' in j and "comment hitkit" in j for j in joined)
    assert any("53, 67" in j or "53," in j for j in joined)


def test_iptables_commands_check_before_insert():
    fwd = firewall.Chain("ip", "filter", "FORWARD", "forward", True)
    for check, add in firewall.commands_for(fwd):
        assert check[:2] == ["iptables", "-C"] and add[:3] == ["iptables", "-I", "FORWARD"]
        assert check[3:] == add[4:]  # same rule spec


def test_apply_script_is_idempotent_shell():
    script = firewall.apply_script(firewall.dropping_chains(CUSTOM, False, False))
    assert script.startswith("#!/bin/sh")
    assert "grep -q 'comment \"hitkit\"'" in script  # nft chains guarded by a check
    assert "iptables -C FORWARD" in script and "|| iptables -I FORWARD 1" in script


def test_existing_hitkit_rules_are_found_and_removable():
    rs = {"nftables": CUSTOM["nftables"] + [
        {"rule": {"family": "inet", "table": "filter", "chain": "input", "handle": 7, "comment": "hitkit", "expr": []}},
        {"rule": {"family": "inet", "table": "filter", "chain": "input", "handle": 8, "expr": []}},
    ]}
    assert firewall.chains_with_hitkit_rules(rs) == {("inet", "filter", "input")}
    assert firewall.removal_commands(rs) == [["nft", "delete", "rule", "inet", "filter", "input", "handle", "7"]]


def test_plan_nftables_as_root_installs_service(monkeypatch, tmp_path):
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup.firewall, "nft_ruleset", lambda: CUSTOM)
    monkeypatch.setattr(setup.firewall, "ufw_active", lambda: False)
    monkeypatch.setattr(setup.firewall, "firewalld_active", lambda: False)
    monkeypatch.setattr(setup.firewall, "SCRIPT_PATH", tmp_path / "hitkit-firewall")
    monkeypatch.setattr(setup.firewall, "UNIT_PATH", tmp_path / "hitkit-firewall.service")
    changes = setup._plan_nftables(remove=False)
    assert len(changes) == 1 and "3 dropping firewall chain(s)" in changes[0].summary
