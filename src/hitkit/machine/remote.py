"""Run commands on machines over SSH with the pinned host key (no TOFU)."""

from __future__ import annotations

from hitkit import trace
from hitkit.addressing import slot_address
from hitkit.errors import HitkitError
from hitkit.machine import creds


def ssh_args(hctx, machine: str, login: str = "root", interactive: bool = False) -> list[str]:
    paths = hctx.paths(machine)
    if not paths.known_hosts.exists():
        raise HitkitError(f"no machine named {machine!r}", hint="hitkit status")
    ip = slot_address(hctx.cfg.network(), machine)
    return [
        "ssh", "-i", str(creds.client_key_path(hctx.user)),
        "-o", "IdentitiesOnly=yes",
        "-o", f"UserKnownHostsFile={paths.known_hosts}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"BatchMode={'no' if interactive else 'yes'}",
        "-o", "LogLevel=ERROR",
        f"{login}@{ip}",
    ]


def run(hctx, machine: str, command: str, stdin: str | None = None) -> str:
    """Run a shell command as root on the machine; secrets go via stdin, never argv."""
    result = trace.run(ssh_args(hctx, machine) + [command], input=stdin, capture_output=True, text=True)
    if result.returncode != 0:
        raise HitkitError(f"command on {machine} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout
