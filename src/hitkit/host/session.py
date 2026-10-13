"""The libvirt session daemon runs in hitkit's own user unit (design §13.13).

libvirt auto-spawns the qemu:///session daemon from whichever client connects
first, inside that client's cgroup, and every VM's QEMU process is the daemon's
child. When that client's unit or scope stops (the pool service restarting, a
terminal or desktop app closing), systemd kills the whole cgroup: the daemon
and all VMs, powered off mid-write. So hitkit starts the daemon itself as
hitkit-libvirt.service, before it ever connects.

Stopping the unit (logout, `systemctl --user stop`) first runs `stop_all`:
every running VM is shut down gracefully, and one that does not finish in time
is saved to disk (resumed by the next `up`). Only then does the daemon get
SIGTERM. libvirt itself saves VMs on host shutdown but not on SIGTERM, and
whatever is left after the daemon exits would be killed.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from hitkit import trace
from hitkit.paths import UserContext

UNIT_NAME = "hitkit-libvirt.service"
_SEARCH = "/usr/sbin:/usr/bin:/sbin:/bin"
_ensured = False
_t = trace.logger(__name__)


def daemon_binary() -> str | None:
    """virtqemud (modular libvirt), else the monolithic libvirtd."""
    return shutil.which("virtqemud", path=_SEARCH) or shutil.which("libvirtd", path=_SEARCH)


def unit_path(user: UserContext) -> Path:
    return user.home / ".config" / "systemd" / "user" / UNIT_NAME


def unit_text(binary: str) -> str:
    import sys

    return (
        "[Unit]\n"
        "Description=libvirt session daemon for hitkit VMs (its own cgroup: no client takes VMs down)\n\n"
        "[Service]\n"
        "Type=notify\n"  # `systemctl start` returns once it listens; a client never spawns a second one
        f"ExecStart={binary}\n"
        f"ExecStop={sys.executable} -m hitkit.host.session stop\n"
        "KillMode=mixed\n"
        "TimeoutStopSec=5min\n"
        "Restart=on-failure\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def daemon_cgroup() -> str | None:
    """The cgroup of the running session daemon of this user, if any."""
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            if Path(os.readlink(proc / "exe")).name not in ("virtqemud", "libvirtd"):
                continue
            return (proc / "cgroup").read_text().strip().rsplit(":", 1)[-1]
        except OSError:
            continue
    return None


def _daemon_pids() -> list[int]:
    pids = []
    for proc in Path("/proc").iterdir():
        try:
            if proc.name.isdigit() and proc.stat().st_uid == os.getuid() and \
                    Path(os.readlink(proc / "exe")).name in ("virtqemud", "libvirtd"):
                pids.append(int(proc.name))
        except OSError:
            continue
    return pids


def _user_vms_running() -> bool:
    for proc in Path("/proc").iterdir():
        try:
            if proc.name.isdigit() and proc.stat().st_uid == os.getuid() and \
                    Path(os.readlink(proc / "exe")).name.startswith("qemu-system"):
                return True
        except OSError:
            continue
    return False


def _terminate_stray_daemon(timeout: float = 10) -> bool:
    import signal
    import time

    for pid in _daemon_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while _daemon_pids() and time.monotonic() < deadline:
        time.sleep(0.2)
    return not _daemon_pids()


def ensure(user: UserContext) -> None:
    """Make sure the session daemon runs as hitkit-libvirt.service. Never fatal: if it
    cannot (no systemd user manager, a stray daemon holding the socket), hitkit still
    connects, and `hitkit doctor` explains the risk."""
    global _ensured
    if _ensured or os.geteuid() != user.uid or os.geteuid() == 0:
        return
    _ensured = True
    binary = daemon_binary()
    if binary is None or shutil.which("systemctl") is None:
        return
    path = unit_path(user)
    text = unit_text(binary)
    try:  # refreshed even while running: systemd applies a reloaded ExecStop at the next stop
        if not path.exists() or path.read_text() != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            trace.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    except OSError as e:
        _t.warning("cannot write the session daemon unit", error=str(e))
        return
    if trace.run(["systemctl", "--user", "is-active", "--quiet", UNIT_NAME]).returncode == 0:
        return
    cgroup = daemon_cgroup()
    if cgroup and UNIT_NAME not in cgroup:
        # Some libvirt client (virsh, virt-manager, an older hitkit) auto-spawned a daemon in
        # its own cgroup. Without VMs it can be replaced safely; with VMs, replacing it would
        # stop them, so it stays and `hitkit doctor` explains how to switch over.
        if _user_vms_running() or not _terminate_stray_daemon():
            _t.warning("session daemon runs inside another unit", cgroup=cgroup)
            return
        _t.info("replaced a client-spawned session daemon (no VMs were running)", cgroup=cgroup)
    trace.run(["systemctl", "--user", "reset-failed", UNIT_NAME], capture_output=True)
    result = trace.run(["systemctl", "--user", "enable", "--now", UNIT_NAME], capture_output=True, text=True)
    if result.returncode != 0:
        _t.warning("cannot start the session daemon unit", stderr=result.stderr.strip())


SHUTDOWN_TIMEOUT = 120


def stop_all(uri: str = "qemu:///session", timeout: float = SHUTDOWN_TIMEOUT) -> None:
    """Gracefully shut down every running VM; save those that do not stop in time.

    Talks to libvirt directly (not hitkit.virt): it runs while the unit is stopping."""
    import time

    import libvirt

    libvirt.registerErrorHandler(lambda _ctx, _err: None, None)
    try:
        conn = libvirt.open(uri)
    except libvirt.libvirtError:
        return  # no daemon reachable: nothing to protect
    try:
        running = [d for d in conn.listAllDomains() if d.isActive()]
        for dom in running:
            try:
                dom.shutdown()
            except libvirt.libvirtError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and any(d.isActive() for d in running):
            time.sleep(1)
        for dom in running:
            if dom.isActive():
                try:
                    dom.managedSave(0)  # keeps every unflushed write; `up` resumes it
                except libvirt.libvirtError:
                    pass
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["stop"]:
        stop_all()
