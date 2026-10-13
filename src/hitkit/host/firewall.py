"""Host firewall integration (design §7.1).

libvirt installs its own accept rules for the hitkit network, but nftables
evaluates every base chain: a `policy drop` (or drop rule) in *any* other
input/forward chain still wins. Guests then lose DNS/DHCP from the network's
dnsmasq and their outbound traffic is not forwarded. hitkit adds the narrowest
rules that fix this, in whichever firewall owns the dropping chain:

- ufw:        `ufw allow ... comment hitkit` rules (ufw persists them)
- firewalld:  the bridge must sit in libvirt's `libvirt` zone (libvirt normally
              does this itself; hitkit verifies and repairs)
- anything else (custom nftables rulesets, iptables-nft such as Docker's
  FORWARD policy DROP): rules tagged `hitkit` inserted at the top of each
  dropping chain, re-applied at boot and on nftables reloads by
  `hitkit-firewall.service`, which setup installs.
"""

from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from hitkit import trace
from hitkit.host.network import BRIDGE_NAME

COMMENT = "hitkit"

# --- ufw -------------------------------------------------------------------

# Each rule is the argument list after `ufw`, without the comment.
RULES: tuple[tuple[str, ...], ...] = (
    ("allow", "in", "on", BRIDGE_NAME, "to", "any", "port", "53", "proto", "udp"),
    ("allow", "in", "on", BRIDGE_NAME, "to", "any", "port", "53", "proto", "tcp"),
    ("allow", "in", "on", BRIDGE_NAME, "to", "any", "port", "67", "proto", "udp"),
    ("route", "allow", "in", "on", BRIDGE_NAME),
)


def _service_active(name: str) -> bool:
    try:
        return trace.run(["systemctl", "is-active", "--quiet", name]).returncode == 0
    except FileNotFoundError:
        return False


def ufw_active() -> bool:
    return _service_active("ufw")


def _normalize(line: str) -> tuple[str, ...]:
    words = line.split()
    if words and words[0] == "ufw":
        words = words[1:]
    if "comment" in words:
        words = words[: words.index("comment")]
    return tuple(words)


def missing_rules(show_added: str) -> list[tuple[str, ...]]:
    """Rules from RULES not present in `ufw show added` output."""
    present = {_normalize(line) for line in show_added.splitlines()}
    return [r for r in RULES if r not in present]


def present_rules(show_added: str) -> list[tuple[str, ...]]:
    present = {_normalize(line) for line in show_added.splitlines()}
    return [r for r in RULES if r in present]


def show_added() -> str:
    """Requires root."""
    return trace.run(["ufw", "show", "added"], capture_output=True, text=True, check=True).stdout


def add(rule: tuple[str, ...]) -> None:
    trace.run(["ufw", *rule, "comment", COMMENT], capture_output=True, check=True)


def delete(rule: tuple[str, ...]) -> None:
    # `ufw delete allow ...` but `ufw route delete allow ...`
    args = ["route", "delete", *rule[1:]] if rule[0] == "route" else ["delete", *rule]
    trace.run(["ufw", *args], capture_output=True, check=True)


# --- firewalld -------------------------------------------------------------

FIREWALLD_ZONE = "libvirt"


def firewalld_active() -> bool:
    return _service_active("firewalld")


def firewalld_zone_of_bridge() -> str | None:
    r = trace.run(["firewall-cmd", f"--get-zone-of-interface={BRIDGE_NAME}"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def firewalld_bind_bridge() -> None:
    """Put the bridge in libvirt's zone, now and permanently. Requires root."""
    for permanent in ([], ["--permanent"]):
        trace.run(["firewall-cmd", *permanent, f"--zone={FIREWALLD_ZONE}",
                        f"--change-interface={BRIDGE_NAME}"], capture_output=True, check=True)


# --- generic nftables / iptables-nft ---------------------------------------

# Tables other mechanisms own: libvirt's own, firewalld's, and (when ufw is
# active) the iptables-nft tables ufw writes into.
_LIBVIRT_TABLES = {"libvirt_network", "libvirt"}


@dataclass(frozen=True)
class Chain:
    family: str  # ip, ip6, inet
    table: str
    name: str
    hook: str  # input | forward
    iptables: bool  # managed through iptables-nft (use iptables, not nft, to edit it)


def dropping_chains(ruleset: dict, ufw: bool, firewalld: bool) -> list[Chain]:
    """Input/forward base chains whose policy is drop, that no other backend handles."""
    chains = []
    for item in ruleset.get("nftables", []):
        chain = item.get("chain")
        if not chain or chain.get("hook") not in ("input", "forward") or chain.get("policy") != "drop":
            continue
        family, table = chain["family"], chain["table"]
        if table in _LIBVIRT_TABLES or table.startswith("libvirt"):
            continue
        if firewalld and table == "firewalld":
            continue
        iptables = family in ("ip", "ip6") and table == "filter" and chain["name"] in ("INPUT", "FORWARD")
        if ufw and iptables:
            continue  # ufw's own chains; handled by the ufw backend
        chains.append(Chain(family, table, chain["name"], chain["hook"], iptables))
    return chains


def chains_with_hitkit_rules(ruleset: dict) -> set[tuple[str, str, str]]:
    """(family, table, chain) for every chain holding a rule commented `hitkit`."""
    found = set()
    for item in ruleset.get("nftables", []):
        rule = item.get("rule")
        if rule and rule.get("comment") == COMMENT:
            found.add((rule["family"], rule["table"], rule["chain"]))
    return found


def commands_for(chain: Chain) -> list[tuple[list[str] | None, list[str]]]:
    """(check, add) argv pairs that open the chain for hitkit's bridge, in order.

    iptables rules are checked individually with `-C`; nft rules have no check
    (the apply script checks the chain for any `hitkit` rule instead).
    """
    if chain.iptables:
        tool = "ip6tables" if chain.family == "ip6" else "iptables"
        if chain.hook == "input":
            specs = [
                ["-i", BRIDGE_NAME, "-p", "udp", "-m", "multiport", "--dports", "53,67"],
                ["-i", BRIDGE_NAME, "-p", "tcp", "--dport", "53"],
            ]
        else:
            specs = [
                ["-i", BRIDGE_NAME],
                ["-o", BRIDGE_NAME, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED"],
            ]
        out = []
        for spec in specs:
            rule = [*spec, "-m", "comment", "--comment", COMMENT, "-j", "ACCEPT"]
            out.append(([tool, "-C", chain.name, *rule], [tool, "-I", chain.name, "1", *rule]))
        return out
    if chain.hook == "input":
        exprs = [
            f'iifname "{BRIDGE_NAME}" udp dport {{ 53, 67 }} accept comment "{COMMENT}"',
            f'iifname "{BRIDGE_NAME}" tcp dport 53 accept comment "{COMMENT}"',
        ]
    else:
        exprs = [
            f'iifname "{BRIDGE_NAME}" accept comment "{COMMENT}"',
            f'oifname "{BRIDGE_NAME}" ct state established,related accept comment "{COMMENT}"',
        ]
    target = [chain.family, chain.table, chain.name]
    return [(None, ["nft", "insert", "rule", *target, *shlex.split(e)]) for e in exprs]


def apply_script(chains: list[Chain]) -> str:
    """A standalone POSIX script that (re)applies the rules; installed for boot and reloads."""
    lines = ["#!/bin/sh", "# Generated by `hitkit setup`; re-run setup to regenerate.", "set -u"]
    for chain in chains:
        cmds = commands_for(chain)
        if chain.iptables:
            # -I 1 puts each rule first, so insert in reverse to keep their order
            for check, add_ in reversed(cmds):
                lines.append(f"{shlex.join(check)} 2>/dev/null || {shlex.join(add_)}")
        else:
            listing = shlex.join(["nft", "list", "chain", chain.family, chain.table, chain.name])
            lines.append(f"if ! {listing} 2>/dev/null | grep -q 'comment \"{COMMENT}\"'; then")
            for _check, add_ in reversed(cmds):
                lines.append(f"    {shlex.join(add_)}")
            lines.append("fi")
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def removal_commands(ruleset: dict) -> list[list[str]]:
    """Delete every rule commented `hitkit` by handle."""
    cmds = []
    for item in ruleset.get("nftables", []):
        rule = item.get("rule")
        if rule and rule.get("comment") == COMMENT and "handle" in rule:
            cmds.append(["nft", "delete", "rule", rule["family"], rule["table"], rule["chain"],
                         "handle", str(rule["handle"])])
    return cmds


def nft_ruleset() -> dict:
    """Requires root."""
    if shutil.which("nft") is None:
        return {"nftables": []}
    out = trace.run(["nft", "-j", "list", "ruleset"], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


SCRIPT_PATH = Path("/usr/local/libexec/hitkit-firewall")
UNIT_PATH = Path("/etc/systemd/system/hitkit-firewall.service")
UNIT = f"""\
[Unit]
Description=Allow hitkit's bridge ({BRIDGE_NAME}) through the host firewall
After=network-pre.target nftables.service iptables.service ip6tables.service docker.service libvirtd.service virtnetworkd.service
PartOf=nftables.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={SCRIPT_PATH}

[Install]
WantedBy=multi-user.target nftables.service
"""
