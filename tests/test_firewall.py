from hitkit.host import firewall

SHOW_ADDED = """Added user rules (see 'ufw status' for running firewall):
ufw allow 22/tcp
ufw allow in on hitkit0 to any port 53 proto udp comment 'hitkit'
ufw route allow in on hitkit0 comment 'hitkit'
"""


def test_missing_rules_ignores_comments_and_unrelated_rules():
    missing = firewall.missing_rules(SHOW_ADDED)
    assert ("allow", "in", "on", "hitkit0", "to", "any", "port", "53", "proto", "tcp") in missing
    assert ("allow", "in", "on", "hitkit0", "to", "any", "port", "67", "proto", "udp") in missing
    assert len(missing) == 2


def test_present_rules_is_the_complement():
    present = firewall.present_rules(SHOW_ADDED)
    assert set(present) | set(firewall.missing_rules(SHOW_ADDED)) == set(firewall.RULES)
    assert ("route", "allow", "in", "on", "hitkit0") in present


def test_no_rules_missing_once_all_added():
    text = "\n".join("ufw " + " ".join(r) + " comment 'hitkit'" for r in firewall.RULES)
    assert firewall.missing_rules(text) == []


def test_delete_uses_route_syntax_for_route_rules(monkeypatch):
    calls = []
    monkeypatch.setattr(firewall.trace, "run", lambda args, **kw: calls.append(args))
    firewall.delete(("route", "allow", "in", "on", "hitkit0"))
    firewall.delete(("allow", "in", "on", "hitkit0", "to", "any", "port", "67", "proto", "udp"))
    assert calls[0][:3] == ["ufw", "route", "delete"]
    assert calls[1][:3] == ["ufw", "delete", "allow"]
