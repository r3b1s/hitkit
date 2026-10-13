"""`hitkit setup`: the one privileged operation (design §7.1).

Planning is read-only and works unprivileged; applying requires root. Every
change is computed up front and shown before anything is touched, and every
step is idempotent, so re-running setup is always safe.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from hitkit import hostinfo, trace, virt
from hitkit.addressing import host_records
from hitkit.config import HostConfig
from hitkit.errors import Conflict, HitkitError, NeedsRoot
from hitkit.host import ca as ca_mod
from hitkit.host import managed_files as mf
from hitkit.host import firewall, network, trust
from hitkit.paths import UserContext


@dataclass(frozen=True)
class Change:
    summary: str
    apply: Callable[[], None]


class NetworkAction(Enum):
    CREATE = "create"
    KEEP = "keep"
    UPDATE_DNS = "update-dns"
    REPLACE = "replace"
    REFUSE = "refuse"


def decide_network_action(
    existing: network.ExistingNetwork | None, desired_subnet: str, force: bool
) -> tuple[NetworkAction, str]:
    """Decide what setup does about the libvirt network named `hitkit`.

    Returns the action and a one-line human-readable reason (shown in the plan,
    or as the error message for REFUSE).
    """
    if existing is None:
        return NetworkAction.CREATE, "not defined yet"
    if not existing.owned:
        if force:
            return NetworkAction.REPLACE, "replacing a network hitkit did not create (--force)"
        return NetworkAction.REFUSE, f"a libvirt network named '{network.NETWORK_NAME}' exists but was not created by hitkit"
    if existing.subnet != desired_subnet:
        if force:
            return NetworkAction.REPLACE, f"moving from {existing.subnet} (--force; existing VMs need re-creating)"
        return NetworkAction.REFUSE, (
            f"the hitkit network uses {existing.subnet}; changing it breaks every existing VM's static address"
        )
    if not existing.matches_desired:
        if existing.dns_only:
            return NetworkAction.UPDATE_DNS, "DNS records changed (updated in place; VMs keep running)"
        note = "; running VMs must be restarted to reattach" if existing.active else ""
        return NetworkAction.REPLACE, f"definition is outdated{note}"
    return NetworkAction.KEEP, "up to date"


def require_root() -> None:
    if os.geteuid() != 0:
        raise NeedsRoot(
            "hitkit setup changes system configuration and must run as root",
            hint="re-run with sudo, or pass --dry-run to only show the plan",
        )


# --- network ---------------------------------------------------------------


def _define_and_start(xml: str, replace: bool) -> None:
    with virt.connect(virt.SYSTEM_URI) as conn:
        if replace:
            old = virt.lookup_network(conn, network.NETWORK_NAME)
            if old is not None:
                xml = network.with_uuid(xml, old.UUIDString())
                if old.isActive():
                    old.destroy()
        net = conn.networkDefineXML(xml)
        net.setAutostart(1)
        if not net.isActive():
            net.create()


def _update_dns(desired_xml: str) -> None:
    libvirt = virt._libvirt()
    flags = libvirt.VIR_NETWORK_UPDATE_AFFECT_CONFIG
    with virt.connect(virt.SYSTEM_URI) as conn:
        net = virt.lookup_network(conn, network.NETWORK_NAME)
        if net.isActive():
            flags |= libvirt.VIR_NETWORK_UPDATE_AFFECT_LIVE
        delete, add = network.dns_changes(net.XMLDesc(0), desired_xml)
        for xml in delete:
            net.update(libvirt.VIR_NETWORK_UPDATE_COMMAND_DELETE, libvirt.VIR_NETWORK_SECTION_DNS_HOST, -1, xml, flags)
        for xml in add:
            net.update(libvirt.VIR_NETWORK_UPDATE_COMMAND_ADD_LAST, libvirt.VIR_NETWORK_SECTION_DNS_HOST, -1, xml, flags)


def _ensure_running() -> None:
    with virt.connect(virt.SYSTEM_URI) as conn:
        net = virt.lookup_network(conn, network.NETWORK_NAME)
        net.setAutostart(1)
        if not net.isActive():
            net.create()


def _existing_network(desired_xml: str) -> network.ExistingNetwork | None:
    with virt.connect(virt.SYSTEM_URI, readonly=True) as conn:
        net = virt.lookup_network(conn, network.NETWORK_NAME)
        if net is None:
            return None
        return network.inspect(net.XMLDesc(0), bool(net.isActive()), desired_xml)


def _plan_network(cfg: HostConfig, force: bool) -> list[Change]:
    desired = network.network_xml(cfg.network())
    existing = _existing_network(desired)
    action, reason = decide_network_action(existing, cfg.subnet, force)
    name = network.NETWORK_NAME
    if action is NetworkAction.REFUSE:
        raise Conflict(reason, hint="pass --force to replace it, or choose another --subnet")
    if action is NetworkAction.CREATE:
        return [Change(f"define libvirt network '{name}' ({cfg.subnet}) — {reason}",
                       lambda: _define_and_start(desired, replace=False))]
    if action is NetworkAction.UPDATE_DNS:
        return [Change(f"update DNS records of libvirt network '{name}' — {reason}",
                       lambda: _update_dns(desired))]
    if action is NetworkAction.REPLACE:
        return [Change(f"replace libvirt network '{name}' ({cfg.subnet}) — {reason}",
                       lambda: _define_and_start(desired, replace=True))]
    if existing is not None and not existing.active:
        return [Change(f"start libvirt network '{name}' and enable autostart", _ensure_running)]
    return []


def _plan_network_removal() -> list[Change]:
    with virt.connect(virt.SYSTEM_URI, readonly=True) as conn:
        net = virt.lookup_network(conn, network.NETWORK_NAME)
        if net is None:
            return []
        existing = network.inspect(net.XMLDesc(0), bool(net.isActive()), net.XMLDesc(0))
    if not existing.owned:
        raise Conflict(f"libvirt network '{network.NETWORK_NAME}' was not created by hitkit")

    def undefine() -> None:
        with virt.connect(virt.SYSTEM_URI) as conn:
            n = virt.lookup_network(conn, network.NETWORK_NAME)
            if n.isActive():
                n.destroy()
            n.undefine()

    return [Change(f"remove libvirt network '{network.NETWORK_NAME}'", undefine)]


# --- firewall --------------------------------------------------------------


def _plan_firewall(remove: bool) -> list[Change]:
    changes = _plan_ufw(remove) + _plan_firewalld(remove)
    changes += _plan_nftables(remove)
    return changes


def _plan_ufw(remove: bool) -> list[Change]:
    if not firewall.ufw_active():
        return []
    if os.geteuid() == 0:
        added = firewall.show_added()
        rules = firewall.present_rules(added) if remove else firewall.missing_rules(added)
    else:  # dry run without root: ufw state is unreadable, so show every rule
        rules = list(firewall.RULES)
    if not rules:
        return []
    if remove:
        return [Change(f"remove {len(rules)} ufw rule(s) for {network.BRIDGE_NAME}",
                       lambda: [firewall.delete(r) for r in rules])]
    return [Change(
        f"allow DNS/DHCP from and forwarding for {network.BRIDGE_NAME} in ufw ({len(rules)} rule(s))",
        lambda: [firewall.add(r) for r in rules],
    )]


def _plan_firewalld(remove: bool) -> list[Change]:
    if remove or not firewall.firewalld_active():
        return []  # the libvirt zone belongs to libvirt; nothing of ours to remove
    if firewall.firewalld_zone_of_bridge() == firewall.FIREWALLD_ZONE:
        return []
    return [Change(f"put {network.BRIDGE_NAME} in firewalld's '{firewall.FIREWALLD_ZONE}' zone",
                   firewall.firewalld_bind_bridge)]


def _install_firewall_service(script: str) -> None:
    mf.write_system_file(firewall.SCRIPT_PATH, script, mode=0o755)
    mf.write_system_file(firewall.UNIT_PATH, firewall.UNIT)
    trace.run(["systemctl", "daemon-reload"], check=True)
    trace.run(["systemctl", "enable", firewall.UNIT_PATH.name], check=True, capture_output=True)
    trace.run(["systemctl", "restart", firewall.UNIT_PATH.name], check=True)


def _remove_firewall_service() -> None:
    if firewall.UNIT_PATH.exists():
        trace.run(["systemctl", "disable", "--now", firewall.UNIT_PATH.name], capture_output=True)
        firewall.UNIT_PATH.unlink()
        trace.run(["systemctl", "daemon-reload"], check=True)
    firewall.SCRIPT_PATH.unlink(missing_ok=True)


def _plan_nftables(remove: bool) -> list[Change]:
    if os.geteuid() != 0:
        return [Change("check other nftables/iptables chains that drop hitkit0 traffic (needs root)", lambda: None)]
    ruleset = firewall.nft_ruleset()
    if remove:
        cmds = firewall.removal_commands(ruleset)
        if not cmds and not firewall.UNIT_PATH.exists():
            return []

        def apply_remove() -> None:
            _remove_firewall_service()
            for cmd in cmds:
                trace.run(cmd, capture_output=True)

        return [Change(f"remove hitkit-firewall.service and {len(cmds)} hitkit firewall rule(s)", apply_remove)]
    chains = firewall.dropping_chains(ruleset, firewall.ufw_active(), firewall.firewalld_active())
    if not chains:
        return [Change("remove hitkit-firewall.service (no longer needed)", _remove_firewall_service)] \
            if firewall.UNIT_PATH.exists() else []
    script = firewall.apply_script(chains)
    covered = firewall.chains_with_hitkit_rules(ruleset)
    if (mf.read_or_empty(firewall.SCRIPT_PATH) == script and firewall.UNIT_PATH.exists()
            and all((c.family, c.table, c.name) in covered for c in chains)):
        return []
    names = ", ".join(f"{c.family} {c.table} {c.name}" for c in chains)
    return [Change(
        f"open {len(chains)} dropping firewall chain(s) for {network.BRIDGE_NAME} ({names}) "
        "and install hitkit-firewall.service to keep them open",
        lambda: _install_firewall_service(script),
    )]


# --- bridge helper ---------------------------------------------------------


def _plan_bridge_helper() -> list[Change]:
    """Session-mode VMs attach to hitkit0 through qemu-bridge-helper, which must be
    setuid root. Arch and Fedora ship it that way; Debian does not."""
    import stat as stat_mod

    helper = hostinfo.bridge_helper()
    if helper is None:
        raise HitkitError("qemu-bridge-helper not found", hint=hostinfo.detect().host_packages)
    st = helper.stat()
    if st.st_mode & stat_mod.S_ISUID and st.st_uid == 0:
        return []

    def apply() -> None:
        if hostinfo.detect().family == "debian" and shutil.which("dpkg-statoverride"):
            # persists across package upgrades
            trace.run(["dpkg-statoverride", "--update", "--add", "root", "root", "4755", str(helper)],
                           check=True, capture_output=True)
        else:
            os.chown(helper, 0, st.st_gid)
            os.chmod(helper, 0o4755)

    return [Change(f"make {helper} setuid root (needed for unprivileged VMs on the bridge)", apply)]


# --- managed files ---------------------------------------------------------


def _plan_file(path, transform, summary: str) -> list[Change]:
    current = mf.read_or_empty(path)
    desired = transform(current)
    if desired == current:
        return []
    return [Change(summary, lambda: mf.write_system_file(path, desired))]


# --- public ----------------------------------------------------------------


def plan_setup(user: UserContext, cfg: HostConfig, *, trust_ca: bool, force: bool) -> list[Change]:
    records = host_records(cfg.network())
    changes = _plan_network(cfg, force)
    changes += _plan_bridge_helper()
    changes += _plan_file(
        mf.BRIDGE_CONF_PATH,
        lambda t: mf.ensure_bridge_allow(t, network.BRIDGE_NAME),
        f"allow bridge '{network.BRIDGE_NAME}' in {mf.BRIDGE_CONF_PATH}",
    )
    changes += _plan_firewall(remove=False)
    changes += _plan_file(
        mf.HOSTS_PATH,
        lambda t: mf.apply_hosts_block(t, records),
        f"write {len(records)} *.hitkit.internal records to {mf.HOSTS_PATH}",
    )
    if trust_ca:
        changes += plan_trust_install(user, cfg)
    return changes


def plan_remove(user: UserContext) -> list[Change]:
    changes = _plan_network_removal()
    changes += _plan_file(
        mf.BRIDGE_CONF_PATH,
        lambda t: mf.remove_bridge_allow(t, network.BRIDGE_NAME),
        f"remove bridge '{network.BRIDGE_NAME}' from {mf.BRIDGE_CONF_PATH}",
    )
    changes += _plan_firewall(remove=True)
    changes += _plan_file(mf.HOSTS_PATH, mf.remove_hosts_block, f"remove hitkit block from {mf.HOSTS_PATH}")
    changes += plan_trust_remove(user)
    return changes


def plan_trust_install(user: UserContext, cfg: HostConfig) -> list[Change]:
    path = ca_mod.ca_cert_path(user)
    if path.exists() and trust.is_installed(path):
        return []

    def apply() -> None:
        ca_mod.ensure_ca(user, cfg.network(), cfg.install_id)
        trust.install(path)

    return [Change(f"add hitkit CA ({path}) to the host trust store", apply)]


def plan_trust_remove(user: UserContext) -> list[Change]:
    path = ca_mod.ca_cert_path(user)
    if not (path.exists() and trust.is_installed(path)):
        return []
    return [Change("remove hitkit CA from the host trust store", lambda: trust.remove(path))]
