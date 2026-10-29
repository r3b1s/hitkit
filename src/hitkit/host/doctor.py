"""`hitkit doctor`: read-only host diagnostics (design §7.4).

Every check returns one CheckResult and never changes the system: no file
writes, no setup calls, no read-write libvirt connections. A check that raises
is reported as a failure rather than crashing the whole run.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from hitkit import trace, virt
from hitkit.config import HostConfig
from hitkit.errors import HitkitError
from hitkit.host import ca
from hitkit.host import managed_files as mf
from hitkit import hostinfo
from hitkit.host import firewall, network, trust
from hitkit.paths import UserContext

Status = Literal["ok", "warn", "fail", "info"]

KVM_PATH = "/dev/kvm"
NESTED_PARAMS = (
    "/sys/module/kvm_intel/parameters/nested",
    "/sys/module/kvm_amd/parameters/nested",
)
BRIDGE_HELPER = Path("/usr/lib/qemu/qemu-bridge-helper")
BUILD_BINARIES = ("mkosi", "qemu-img", "apt")
BUILD_FILES = (
    "/usr/share/edk2/x64/OVMF_CODE.4m.fd",
    "/usr/share/keyrings/debian-archive-keyring.gpg",
)
GIB = 1024**3


@dataclass(frozen=True)
class CheckResult:
    name: str  # stable machine id, e.g. "kvm"
    status: Status
    message: str  # one line, human readable
    hint: str | None = None  # remediation, only for warn/fail


def run_checks(user: UserContext, cfg: HostConfig | None) -> list[CheckResult]:
    plan: list[tuple[str, Callable[..., CheckResult], tuple[Any, ...]]] = [
        ("kvm", check_kvm, ()),
        ("nested-virt", check_nested_virt, ()),
        ("libvirt-session", check_libvirt_session, ()),
        ("network", check_network, (cfg,)),
        ("bridge-helper", check_bridge_helper, ()),
        ("bridge-conf", check_bridge_conf, ()),
        ("hosts-file", check_hosts_file, ()),
        ("subnet-collision", check_subnet_collision, (cfg,)),
        ("firewalld", check_firewalld, ()),
        ("ufw", check_ufw, ()),
        ("firewall-service", check_firewall_service, ()),
        ("build-tools", check_build_tools, ()),
        ("swtpm", check_swtpm, ()),
        ("secure-boot-ovmf", check_secure_boot_ovmf, ()),
        ("host-trust", check_host_trust, (user,)),
        ("install-id", check_install_id, (user, cfg)),
        ("session-daemon", check_session_daemon, ()),
        ("disk-capacity", check_disk_capacity, (user,)),
        ("resources", check_resources, (user,)),
    ]
    return [_guard(name, fn, *args) for name, fn, args in plan]


def _guard(name: str, fn: Callable[..., CheckResult], *args: Any) -> CheckResult:
    try:
        return fn(*args)
    except Exception as e:
        return CheckResult(name, "fail", f"unexpected error: {type(e).__name__}: {e}")


def check_kvm() -> CheckResult:
    if os.path.exists(KVM_PATH) and os.access(KVM_PATH, os.R_OK | os.W_OK):
        return CheckResult("kvm", "ok", f"{KVM_PATH} is accessible")
    return CheckResult(
        "kvm",
        "fail",
        f"{KVM_PATH} is missing or not readable and writable",
        hint="enable virtualization (VT-x/AMD-V) in firmware, and add your user to the kvm group",
    )


def check_nested_virt() -> CheckResult:
    for path in NESTED_PARAMS:
        try:
            value = Path(path).read_text().strip()
        except FileNotFoundError:
            continue
        if value in ("Y", "1"):
            return CheckResult("nested-virt", "ok", "nested virtualization is enabled")
        return CheckResult(
            "nested-virt",
            "warn",
            "nested virtualization is disabled (needed later for the proxmox kit)",
            hint="enable nested=1 for kvm_intel or kvm_amd",
        )
    return CheckResult(
        "nested-virt",
        "warn",
        "cannot read nested virtualization state (kvm module parameter missing)",
        hint="load the kvm_intel or kvm_amd module (needed later for the proxmox kit)",
    )


def check_libvirt_session() -> CheckResult:
    try:
        with virt.connect(virt.SESSION_URI, readonly=True):
            pass
    except Exception as e:
        return CheckResult("libvirt-session", "fail", f"cannot connect to {virt.SESSION_URI}: {e}")
    return CheckResult("libvirt-session", "ok", f"{virt.SESSION_URI} is reachable")


def check_network(cfg: HostConfig | None) -> CheckResult:
    try:
        return _check_network(cfg)
    except HitkitError as e:  # libvirt unreachable: report it as a normal failure
        return CheckResult("network", "fail", str(e), hint=e.hint)


def _check_network(cfg: HostConfig | None) -> CheckResult:
    name = "network"
    with virt.connect(virt.SYSTEM_URI, readonly=True) as conn:
        net = virt.lookup_network(conn, network.NETWORK_NAME)
        if net is None:
            return CheckResult(
                name,
                "fail",
                f"libvirt network '{network.NETWORK_NAME}' is not defined",
                hint="sudo hitkit setup",
            )
        active = bool(net.isActive())
        if cfg is None:
            if not active:
                return CheckResult(name, "fail", f"network '{network.NETWORK_NAME}' is not active",
                                   hint="sudo hitkit setup")
            return CheckResult(name, "ok", f"network '{network.NETWORK_NAME}' is active")
        existing = network.inspect(net.XMLDesc(), active, network.network_xml(cfg.network()))
        autostart = bool(net.autostart())  # read while the connection is still open

    if not existing.owned:
        return CheckResult(
            name,
            "fail",
            f"network '{network.NETWORK_NAME}' exists but was not created by hitkit",
            hint="sudo hitkit setup --force to replace it",
        )
    if not existing.active:
        return CheckResult(name, "fail", f"network '{network.NETWORK_NAME}' is not active",
                           hint="sudo hitkit setup")
    if not existing.matches_desired:
        return CheckResult(
            name,
            "warn",
            f"network '{network.NETWORK_NAME}' differs from the desired definition",
            hint="sudo hitkit setup",
        )
    if not autostart:
        return CheckResult(
            name,
            "warn",
            f"network '{network.NETWORK_NAME}' does not autostart",
            hint="sudo hitkit setup",
        )
    return CheckResult(name, "ok", f"network '{network.NETWORK_NAME}' is active on {existing.subnet}")


def check_bridge_helper() -> CheckResult:
    name = "bridge-helper"
    helper = hostinfo.bridge_helper()
    if helper is None:
        return CheckResult(name, "fail", "qemu-bridge-helper not found",
                           hint=hostinfo.detect().host_packages)
    st = helper.stat()
    if st.st_mode & stat.S_ISUID and st.st_uid == 0:
        return CheckResult(name, "ok", f"{helper} is setuid root")
    return CheckResult(name, "fail", f"{helper} is not setuid root", hint="sudo hitkit setup")


def check_bridge_conf() -> CheckResult:
    text = mf.read_or_empty(mf.BRIDGE_CONF_PATH)
    if mf.has_bridge_allow(text, network.BRIDGE_NAME):
        return CheckResult("bridge-conf", "ok", f"{network.BRIDGE_NAME} is allowed in {mf.BRIDGE_CONF_PATH}")
    return CheckResult(
        "bridge-conf",
        "fail",
        f"'allow {network.BRIDGE_NAME}' is missing from {mf.BRIDGE_CONF_PATH}",
        hint="sudo hitkit setup",
    )


def check_hosts_file() -> CheckResult:
    text = mf.read_or_empty(mf.HOSTS_PATH)
    if mf.BEGIN in text.splitlines():
        return CheckResult("hosts-file", "ok", f"hitkit block present in {mf.HOSTS_PATH}")
    return CheckResult(
        "hosts-file",
        "warn",
        f"hitkit block missing from {mf.HOSTS_PATH}",
        hint="sudo hitkit setup",
    )


def check_subnet_collision(cfg: HostConfig | None) -> CheckResult:
    name = "subnet-collision"
    if cfg is None:
        return CheckResult(name, "info", "no hitkit config yet")
    out = trace.run(
        ["ip", "-j", "route", "show"], capture_output=True, text=True, check=True
    ).stdout
    conflicts = find_route_conflicts(json.loads(out), cfg.network(), network.BRIDGE_NAME)
    if conflicts:
        return CheckResult(
            name,
            "fail",
            f"subnet {cfg.subnet} overlaps existing routes: {'; '.join(conflicts)}",
            hint="sudo hitkit setup --subnet <another private /24>",
        )
    return CheckResult(name, "ok", f"subnet {cfg.subnet} does not collide with host routes")


def find_route_conflicts(
    routes: list[dict], subnet: ipaddress.IPv4Network, own_dev: str
) -> list[str]:
    """Describe every route outside ``own_dev`` whose destination overlaps ``subnet``."""
    conflicts = []
    for route in routes:
        dst = route.get("dst")
        if not dst or dst == "default" or route.get("dev") == own_dev:
            continue
        try:
            dest = ipaddress.IPv4Network(dst if "/" in dst else f"{dst}/32", strict=False)
        except ValueError:
            continue  # not an IPv4 destination
        if dest.overlaps(subnet):
            conflicts.append(f"{dst} dev {route.get('dev', '?')}")
    return conflicts


def check_firewalld() -> CheckResult:
    name = "firewalld"
    if firewall.firewalld_active():
        zone = firewall.firewalld_zone_of_bridge()
        if zone == firewall.FIREWALLD_ZONE:
            return CheckResult(name, "ok", f"firewalld active; {network.BRIDGE_NAME} is in the libvirt zone")
        return CheckResult(name, "warn", f"firewalld active; {network.BRIDGE_NAME} is in zone {zone or 'none'}, not libvirt",
                           hint="sudo hitkit setup")
    return CheckResult(name, "ok", "firewalld is not active")


def check_ufw() -> CheckResult:
    # ufw's rule state is root-only; setup owns the rules, the guest's dns gate verifies them.
    if firewall.ufw_active():
        return CheckResult("ufw", "info", f"ufw is active; `sudo hitkit setup` manages rules for {network.BRIDGE_NAME}")
    return CheckResult("ufw", "ok", "ufw is not active")


def check_firewall_service() -> CheckResult:
    """Other dropping chains (custom nftables, Docker) are opened by hitkit-firewall.service."""
    name = "firewall-service"
    if firewall.UNIT_PATH.exists():
        if firewall._service_active(firewall.UNIT_PATH.name):
            return CheckResult(name, "ok", "hitkit-firewall.service keeps custom firewall chains open for hitkit0")
        return CheckResult(name, "fail", "hitkit-firewall.service is installed but not active",
                           hint="sudo systemctl restart hitkit-firewall.service, or re-run sudo hitkit setup")
    others = [s for s in ("nftables", "docker", "iptables") if firewall._service_active(s)]
    if others:
        return CheckResult(name, "info", f"{', '.join(others)} active; `sudo hitkit setup` inspects their "
                           "chains and opens hitkit0 if any of them drop by default")
    return CheckResult(name, "ok", "no other firewall services detected")


def check_build_tools() -> CheckResult:
    distro = hostinfo.detect()
    missing = [t for t in ("qemu-img",) if shutil.which(t) is None]
    if hostinfo.mkosi_binary() is None:
        missing.append("mkosi")
    if not hostinfo.ovmf_available():
        missing.append("OVMF firmware")
    if missing:
        return CheckResult("build-tools", "fail", f"missing: {', '.join(missing)}", hint=distro.host_packages)
    if problem := hostinfo.mkosi_problem(hostinfo.mkosi_version()):
        return CheckResult("build-tools", "fail", problem, hint=hostinfo.MKOSI_UPGRADE_HINT)
    tree = [d for d in ("debian", "arch") if not hostinfo.can_build_natively(d)]
    if tree:
        return CheckResult("build-tools", "ok",
                           f"mkosi present; {'/'.join(tree)} images will use mkosi's tools tree (first build downloads it)")
    return CheckResult("build-tools", "ok", "image build tools are present")


def check_swtpm() -> CheckResult:
    name = "swtpm"
    distro = hostinfo.detect()
    if shutil.which("swtpm", path=hostinfo._SEARCH_PATH) is None:
        return CheckResult(
            name,
            "warn",
            "swtpm is not installed (needed only for the incus-os kit)",
            hint=distro.host_packages,
        )
    if distro.family in ("debian", "fedora") and shutil.which("swtpm_setup", path=hostinfo._SEARCH_PATH) is None:
        return CheckResult(
            name,
            "warn",
            "swtpm-tools is not installed (needed only for the incus-os kit)",
            hint=distro.host_packages,
        )
    return CheckResult(name, "ok", "swtpm is present")


def check_secure_boot_ovmf() -> CheckResult:
    name = "secure-boot-ovmf"
    if hostinfo.secure_boot_ovmf_available():
        return CheckResult(name, "ok", "secure-boot OVMF firmware descriptor is present")
    return CheckResult(
        name,
        "warn",
        "secure-boot OVMF firmware descriptor not found (needed only for the incus-os kit)",
        hint=hostinfo.detect().host_packages,
    )


def check_host_trust(user: UserContext) -> CheckResult:
    name = "host-trust"
    path = ca.ca_cert_path(user)
    if not path.exists():
        return CheckResult(name, "info", "hitkit CA not created yet")
    if trust.is_installed(path):
        return CheckResult(name, "info", "CA installed in host trust store")
    return CheckResult(name, "info", "CA not in host trust store (optional: sudo hitkit trust install)")


def check_session_daemon() -> CheckResult:
    """VMs are children of the session daemon: they live and die with its cgroup."""
    from hitkit.host import session

    cgroup = session.daemon_cgroup()
    if cgroup is None:
        return CheckResult("session-daemon", "info", f"not running (hitkit starts {session.UNIT_NAME} when needed)")
    if session.UNIT_NAME in cgroup:
        return CheckResult("session-daemon", "ok", f"runs as {session.UNIT_NAME}")
    unit = cgroup.rsplit("/", 1)[-1]
    return CheckResult(
        "session-daemon", "warn",
        f"libvirt's session daemon runs inside {unit}: if that stops, every VM is killed",
        hint=f"with all VMs stopped (hitkit down <kit>), run: systemctl --user stop {unit}; "
             f"the next hitkit command starts {session.UNIT_NAME}",
    )


def check_install_id(user: UserContext, cfg: HostConfig | None) -> CheckResult:
    """Machines naming another install ID are invisible to hitkit (and were once at risk from gc)."""
    from hitkit import config

    installs = config.discover(user)
    if cfg is None:
        if installs:
            return CheckResult("install-id", "warn", "hitkit is not configured but hitkit machines exist",
                               hint="sudo hitkit setup (recovers their install ID)")
        return CheckResult("install-id", "info", "no hitkit machines yet")
    others = {iid: i for iid, i in installs.items() if iid != cfg.install_id}
    if not others:
        mine = installs.get(cfg.install_id)
        return CheckResult("install-id", "ok", f"{len(mine.machines) if mine else 0} machine(s) on install {cfg.install_id[:8]}")
    names = "; ".join(f"{', '.join(i.machines)} ({iid[:8]})" for iid, i in others.items())
    return CheckResult(
        "install-id", "warn", f"machines owned by another install ID: {names}",
        hint="hitkit leaves them alone; if they are yours, restore that ID in ~/.config/hitkit/config.toml",
    )


def check_disk_capacity(user: UserContext) -> CheckResult:
    """Data disks are thin: their sizes are caps. Warn when the caps could outgrow the host."""
    from hitkit.machine import disks

    vms = user.data_dir / "vms"
    caps = used = 0
    for disk in list(vms.glob("*/data.qcow2")) + list(vms.glob(".kept/*.qcow2")):
        try:
            d = disks.info(disk)
        except HitkitError:
            continue
        caps += d["virtual-size"]
        used += d["actual-size"]
    free = shutil.disk_usage(_nearest_existing(vms)).free
    growth = caps - used
    message = f"data disks: {used / GIB:.0f} GiB used of {caps / GIB:.0f} GiB allowed; {free / GIB:.0f} GiB free"
    if growth > free:
        return CheckResult("disk-capacity", "warn", message,
                           hint=f"if every data disk filled up, the host would run out of space "
                                f"({(growth - free) / GIB:.0f} GiB short)")
    return CheckResult("disk-capacity", "ok", message)


def check_resources(user: UserContext) -> CheckResult:
    """Informational only: never warns or fails (design §7.4, §9.5)."""
    cpus = os.cpu_count() or "unknown"
    ram = _total_ram_gib()
    ram_text = f"{ram:.1f} GiB" if ram is not None else "unknown"
    anchor = _nearest_existing(user.data_dir)
    free = shutil.disk_usage(anchor).free / GIB
    return CheckResult(
        "resources",
        "info",
        f"{cpus} CPUs, {ram_text} RAM, {free:.1f} GiB free disk at {anchor}",
    )


def _total_ram_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _nearest_existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path
