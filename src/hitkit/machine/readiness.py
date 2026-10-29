"""Readiness reported by the guest on its serial console (design §8.6).

The guest's hitkit-ready.service prints exactly one of:

    HITKIT-READY
    HITKIT-FAILED gate=<name> detail=<text>
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from hitkit import trace
from hitkit.errors import HitkitError

_t = trace.logger(__name__)
CONSOLE_TAIL_LINES = 60

_FAILED = re.compile(r"HITKIT-FAILED gate=(\S+)(?: detail=(.*))?")


@dataclass(frozen=True)
class Ready:
    pass


@dataclass(frozen=True)
class Failed:
    gate: str
    detail: str


_READY = re.compile(r"(?:^|\s)HITKIT-READY\s*$")


def scan(text: str) -> Ready | Failed | None:
    """Find the guest's verdict in console output. The last marker wins.

    Markers may share a line with other console output (e.g. getty's login
    prompt has no trailing newline), so they are matched anywhere in a line.
    """
    verdict: Ready | Failed | None = None
    for line in text.splitlines():
        line = line.replace("\r", "")
        if _READY.search(line):
            verdict = Ready()
        elif m := _FAILED.search(line):
            verdict = Failed(m.group(1), (m.group(2) or "").strip())
    return verdict


class NotReady(HitkitError):
    pass


def wait(
    console_log: Path,
    timeout: float,
    is_running: Callable[[], bool],
    poll: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Block until the guest reports ready; raise NotReady on failure, exit, or timeout.

    A failure is recorded in the trace together with the end of the console log."""
    started = clock()
    deadline = started + timeout

    def not_ready(message: str, hint: str, **fields) -> NotReady:
        try:
            tail = console_log.read_text(errors="replace").replace("\r", "").splitlines()[-CONSOLE_TAIL_LINES:]
        except FileNotFoundError:
            tail = []
        _t.error("machine not ready", reason=message, console_log=console_log,
                 waited_s=round(clock() - started), console_tail="\n".join(tail), **fields)
        return NotReady(message, hint=hint)

    while True:
        try:
            verdict = scan(console_log.read_text(errors="replace"))
        except FileNotFoundError:
            verdict = None
        if isinstance(verdict, Ready):
            _t.info("machine ready", console_log=console_log, waited_s=round(clock() - started))
            return
        if isinstance(verdict, Failed):
            raise not_ready(
                f"guest reported failure at gate '{verdict.gate}': {verdict.detail}",
                f"see the full boot log: {console_log}", gate=verdict.gate,
            )
        if not is_running():
            raise not_ready("machine stopped before reporting ready", f"see {console_log}")
        if clock() >= deadline:
            raise not_ready(f"no readiness report after {timeout:.0f}s", f"see {console_log}")
        sleep(poll)


# --- installer machines (plan §4.4) ----------------------------------------

# Console text that means "installed; remove the media". The TUI draws it with escape codes
# in between, so only phrases seen intact on the console count (docs/incus/incus-os-in-libvirt.md
# §4). The second is IncusOS booting its install media again after a finished install.
INSTALL_PATTERNS = (
    "was successfully installed.",
    "install media detected, but the system is already installed",
)


def scan_installer(text: str) -> bool:
    return any(p in text for p in INSTALL_PATTERNS)


def wait_installer(
    console_log: Path,
    timeout: float,
    is_running: Callable[[], bool],
    poll: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for the installer to finish in console.log."""
    started = clock()
    deadline = started + timeout

    def not_ready(message: str, hint: str) -> NotReady:
        try:
            tail = console_log.read_text(errors="replace").replace("\r", "").splitlines()[-CONSOLE_TAIL_LINES:]
        except FileNotFoundError:
            tail = []
        _t.error("installer machine not ready", reason=message, console_log=console_log,
                 waited_s=round(clock() - started), console_tail="\n".join(tail))
        return NotReady(message, hint=hint)

    while True:
        try:
            text = console_log.read_text(errors="replace")
        except FileNotFoundError:
            text = ""
        if scan_installer(text):
            _t.info("installer finished", console_log=console_log, waited_s=round(clock() - started))
            return
        if not is_running():
            raise not_ready("machine stopped before install completed", f"see {console_log}")
        if clock() >= deadline:
            raise not_ready(f"installer did not complete after {timeout:.0f}s", f"see {console_log}")
        sleep(poll)
