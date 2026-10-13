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
