"""Traceability: structured debug logs that let a failure be reconstructed later.

Every invocation gets a trace id (or inherits ``HITKIT_TRACE_ID``). Records go,
always at debug detail, to one JSON-lines file per day under
``$XDG_STATE_HOME/hitkit/logs`` (kept RETENTION_DAYS days); ``--debug`` or
``HITKIT_DEBUG=1`` also mirrors them to stderr. Errors print the trace id, and
``hitkit trace ID`` shows what happened.

Secrets never reach a record: every value the secret store hands out is
registered and masked wherever it appears, as are PEM private keys, URL
userinfo, Authorization headers, and fields named like secrets. Logging must
never break hitkit: if the log file cannot be written, records are dropped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets as _random
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

ROOT = "hitkit"
RETENTION_DAYS = 14
MASK = "***"
STDERR_TAIL = 2000  # characters of a failed command's output kept in its record

_state = {"trace": "", "cmd": ""}
_known: set[str] = set()

_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
     "-----BEGIN PRIVATE KEY-----" + MASK),
    (re.compile(r"(?i)\b(authorization:\s*)(\S+\s+)?\S+"), r"\1" + MASK),
    (re.compile(r"(?i)\b(token|basic|bearer|PVEAPIToken)([ =])[A-Za-z0-9._~+/=!@:-]{8,}"), r"\1\2" + MASK),
    (re.compile(r"(?i)(https?://[^/\s:@]+:)[^@\s/]+@"), r"\1" + MASK + "@"),
    (re.compile(r'(?i)("(?:[a-z_]*_)?(?:token|password|secret|sha1)"\s*:\s*")[^"]*"'), r"\1" + MASK + '"'),
]
_SECRET_KEY = re.compile(r"(?i)(^|[_.-])(token|password|passwd|secret|authorization|private_key)$")


def new_id() -> str:
    return _random.token_hex(6)


def trace_id() -> str:
    return _state["trace"]


def secret(value: str | bytes | None) -> None:
    """Register a secret value so it is masked in every later record."""
    if isinstance(value, bytes):
        try:
            value = value.decode()
        except UnicodeDecodeError:
            return
    if not isinstance(value, str):
        return
    value = value.strip()
    if len(value) >= 8 and not any(c.isspace() for c in value):
        _known.add(value)


def redact(value):
    if isinstance(value, str):
        for known in sorted(_known, key=len, reverse=True):
            if known in value:
                value = value.replace(known, MASK)
        for pattern, repl in _PATTERNS:
            value = pattern.sub(repl, value)
        return value
    if isinstance(value, dict):
        return {k: MASK if _SECRET_KEY.search(str(k)) and v not in (None, "") else redact(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


# --- records ------------------------------------------------------------------


class Logger:
    """`logging.Logger` with keyword fields: ``log.info("defined", machine="forgejo")``."""

    def __init__(self, name: str):
        self._logger = logging.getLogger(name)

    def _log(self, level: int, msg: str, exc_info=None, **fields) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(level, msg, extra={"fields": fields}, exc_info=exc_info, stacklevel=3)

    def debug(self, msg: str, exc_info=None, **fields) -> None:
        self._log(logging.DEBUG, msg, exc_info, **fields)

    def info(self, msg: str, exc_info=None, **fields) -> None:
        self._log(logging.INFO, msg, exc_info, **fields)

    def warning(self, msg: str, exc_info=None, **fields) -> None:
        self._log(logging.WARNING, msg, exc_info, **fields)

    def error(self, msg: str, exc_info=None, **fields) -> None:
        self._log(logging.ERROR, msg, exc_info, **fields)


def logger(name: str) -> Logger:
    return Logger(name if name.startswith(ROOT) else f"{ROOT}.{name}")


_log = logger("proc")


def _record_dict(record: logging.LogRecord) -> dict:
    data = {
        "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
        "level": record.levelname.lower(),
        "trace": _state["trace"],
        "cmd": _state["cmd"],
        "pid": record.process,
        "logger": record.name,
        "msg": redact(record.getMessage()),
    }
    fields = getattr(record, "fields", None)
    if fields:
        data.update(redact(fields))
    if record.exc_info:
        data["exc"] = redact("".join(traceback.format_exception(*record.exc_info)))
    return data


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_record_dict(record), default=str)


class _HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return format_human(_record_dict(record))


def local_time(ts: str | None, spec: str = "%H:%M:%S.%f") -> str:
    """A record's UTC timestamp in the viewer's local time."""
    try:
        text = datetime.fromisoformat(ts).astimezone().strftime(spec)
    except (TypeError, ValueError):
        return ts or ""
    return text[:-3] if spec.endswith("%f") else text


def format_human(rec: dict) -> str:
    core = ("ts", "level", "trace", "cmd", "pid", "logger", "msg", "exc")
    fields = " ".join(f"{k}={json.dumps(v) if not isinstance(v, str) or ' ' in v else v}"
                      for k, v in rec.items() if k not in core)
    line = f"{local_time(rec.get('ts'))} {rec.get('level', '')[:1].upper()} " \
           f"{rec.get('logger', '').removeprefix(ROOT + '.'):<16} {rec.get('msg', '')}"
    line = f"{line}  {fields}" if fields else line
    return f"{line}\n{rec['exc'].rstrip()}" if rec.get("exc") else line


class _DailyFile(logging.Handler):
    """Appends whole lines to <dir>/<YYYY-MM-DD>.jsonl, safe across concurrent processes."""

    def __init__(self, directory: Path, owner: tuple[int, int] | None):
        super().__init__(logging.DEBUG)
        self.directory, self.owner = directory, owner
        self._day, self._fd = "", -1

    def _fd_for_today(self) -> int:
        day = date.today().isoformat()
        if day != self._day:
            if self._fd >= 0:
                os.close(self._fd)
            path = self.directory / f"{day}.jsonl"
            self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            if self.owner:
                os.fchown(self._fd, *self.owner)
            self._day = day
        return self._fd

    def emit(self, record: logging.LogRecord) -> None:
        try:
            os.write(self._fd_for_today(), (self.format(record) + "\n").encode())
        except Exception:
            pass  # never let tracing break the operation being traced

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        super().close()


def log_dir(user) -> Path:
    return user.state_dir / "logs"


def setup(user, cmd: str, debug: bool = False, file_level: int = logging.DEBUG) -> str:
    """Start this process's trace; returns its id. Safe to call again (tests, pool ticks)."""
    from hitkit.paths import ensure_private_dir

    _state["trace"] = os.environ.get("HITKIT_TRACE_ID") or new_id()
    _state["cmd"] = cmd
    root = logging.getLogger(ROOT)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG)
    root.propagate = False
    if user is not None:
        try:
            directory = ensure_private_dir(log_dir(user), user)
            handler = _DailyFile(directory, (user.uid, user.gid) if os.geteuid() == 0 else None)
            handler.setLevel(file_level)
            handler.setFormatter(_JsonFormatter())
            root.addHandler(handler)
            _prune(directory)
        except OSError:
            pass
    if debug:
        stderr = logging.StreamHandler(sys.stderr)
        stderr.setLevel(logging.DEBUG)
        stderr.setFormatter(_HumanFormatter())
        root.addHandler(stderr)
    if not root.handlers:
        root.addHandler(logging.NullHandler())  # not logging.lastResort's stderr output
    return _state["trace"]


def new_trace(cmd: str | None = None) -> str:
    """Begin a fresh trace within a long-running process (one per pool tick)."""
    _state["trace"] = new_id()
    if cmd is not None:
        _state["cmd"] = cmd
    return _state["trace"]


def _prune(directory: Path) -> None:
    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    for path in directory.glob("*.jsonl"):
        if path.stem < cutoff:
            path.unlink(missing_ok=True)


# --- spans and commands ---------------------------------------------------------


@contextmanager
def span(name: str, **fields) -> Iterator[dict]:
    """Time a step. Yields a dict whose entries are added to the closing record."""
    span_log = logger(f"{ROOT}.span")
    extra: dict = {}
    span_log.debug(f"{name} started", span=name, **fields)
    started = time.monotonic()
    try:
        yield extra
    except BaseException as e:
        ms = round((time.monotonic() - started) * 1000)
        span_log.error(f"{name} failed", span=name, duration_ms=ms, error=f"{type(e).__name__}: {e}",
                       **{**fields, **extra})
        raise
    span_log.info(f"{name} done", span=name, duration_ms=round((time.monotonic() - started) * 1000),
                  **{**fields, **extra})


def _tail(text: str | bytes | None) -> str | None:
    if text is None:
        return None
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    text = text.strip()
    return text[-STDERR_TAIL:] if text else None


def run(cmd, *, check: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` that records the command, its duration, and how it ended.

    stdin content is never recorded (only its size); a failure's output tail is.
    """
    argv = [str(c) for c in cmd]
    fields = {"argv": argv}
    if kwargs.get("input") is not None:
        fields["stdin_bytes"] = len(kwargs["input"])
    if "cwd" in kwargs:
        fields["cwd"] = str(kwargs["cwd"])
    started = time.monotonic()
    try:
        result = subprocess.run(argv, **kwargs)
    except OSError as e:
        _log.warning("command could not start", error=str(e), **fields)
        raise
    fields["rc"] = result.returncode
    fields["duration_ms"] = round((time.monotonic() - started) * 1000)
    if result.returncode == 0:
        _log.debug("command ok", **fields)
    else:
        _log.info("command failed", stderr=_tail(result.stderr), stdout=_tail(result.stdout), **fields)
    if check:
        result.check_returncode()
    return result


# --- reading traces back ----------------------------------------------------------


def read(user) -> Iterator[dict]:
    """Every record still on disk, oldest first. Malformed lines are skipped."""
    for path in sorted(log_dir(user).glob("*.jsonl")):
        with path.open(errors="replace") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("trace"):
                    yield record


def summarize(records) -> list[dict]:
    """One row per trace, in order of first appearance."""
    traces: dict[str, dict] = {}
    for r in records:
        t = traces.setdefault(r["trace"], {"trace": r["trace"], "start": r.get("ts"), "cmd": r.get("cmd"),
                                           "records": 0, "errors": 0, "exit_code": None, "duration_ms": None})
        t["end"] = r.get("ts")
        t["records"] += 1
        if r.get("level") in ("error", "critical"):
            t["errors"] += 1
        if r.get("logger") == f"{ROOT}.cli" and "exit_code" in r:
            t["exit_code"], t["duration_ms"] = r["exit_code"], r.get("duration_ms")
    return list(traces.values())


def failed(summary: dict) -> bool:
    return summary["errors"] > 0 or summary["exit_code"] not in (None, 0)


def select(user, ref: str, only_failed: bool = False) -> list[dict]:
    """The records of one trace: an id, a unique id prefix, or "last"."""
    from hitkit.errors import Conflict, HitkitError

    records = list(read(user))
    rows = [t for t in summarize(records) if failed(t) or not only_failed]
    if ref == "last":
        if not rows:
            raise HitkitError("no traces recorded yet")
        wanted = rows[-1]["trace"]
    else:
        ids = [t["trace"] for t in rows if t["trace"].startswith(ref)]
        if not ids:
            raise HitkitError(f"no trace {ref!r} (traces are kept {RETENTION_DAYS} days)", hint="hitkit trace")
        if len(ids) > 1:
            raise Conflict(f"trace prefix {ref!r} is ambiguous: {', '.join(ids[:5])}")
        wanted = ids[0]
    return [r for r in records if r["trace"] == wanted]
