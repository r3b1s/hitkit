"""Traceability: trace records, secret redaction, spans, commands, and `hitkit trace`."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
from datetime import date, timedelta

import pytest
from click.testing import CliRunner

from hitkit import cli, secrets, trace
from hitkit.errors import Conflict, HitkitError


def records(user) -> list[dict]:
    return list(trace.read(user))


@pytest.fixture
def traced(user, monkeypatch):
    monkeypatch.delenv("HITKIT_TRACE_ID", raising=False)
    trace.setup(user, "test cmd")
    return user


# --- redaction --------------------------------------------------------------------


def test_registered_secrets_are_masked_everywhere():
    trace.secret("s3cr3t-value-123")

    assert trace.redact("cmd --x s3cr3t-value-123 y") == "cmd --x *** y"
    assert trace.redact({"argv": ["a", "pre-s3cr3t-value-123"]}) == {"argv": ["a", "pre-***"]}


def test_short_or_multiword_values_are_not_registered():
    for value in ("short", "two words here", "", None, 1234567890):
        trace.secret(value)

    assert trace._known == set()


@pytest.mark.parametrize("text,leak", [
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----", "AAAA"),
    ("Authorization: token abcdef0123456789", "abcdef0123456789"),
    ("https://oauth2:glpat-abcdefgh123@gitlab.example/x.git", "glpat-abcdefgh123"),
    ('{"name": "t", "sha1": "deadbeefdeadbeef"}', "deadbeefdeadbeef"),
    ("Authorization: PVEAPIToken=root@pam!hitkit=1234-5678-90ab", "1234-5678-90ab"),
])
def test_secret_shapes_are_masked_without_registration(text, leak):
    assert leak not in trace.redact(text)


def test_fields_named_like_secrets_are_masked_but_names_are_not():
    out = trace.redact({"password": "pw", "admin_token": "t", "credentials": ["hitkit.fj.secret-key"]})

    assert out == {"password": "***", "admin_token": "***", "credentials": ["hitkit.fj.secret-key"]}


def test_secret_store_registers_values_but_not_bookkeeping_json(user):
    secrets.put(user, "forge", "admin.token", "tok-0123456789")
    secrets.put(user, "forge", "knobs.json", '{"runners":1}')

    assert "tok-0123456789" in trace._known
    assert '{"runners":1}' not in trace._known


def test_forgejo_responses_register_minted_tokens(monkeypatch, tmp_path):
    import ipaddress

    from hitkit import forgejo
    from hitkit.host.ca import create_ca

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": 1, "sha1": "minted-0123456789", "token": "reg-0123456789"}'

    monkeypatch.setattr(forgejo.urllib.request, "urlopen", lambda req, context=None, timeout=None: Response())
    cafile = tmp_path / "ca.crt"
    cafile.write_bytes(create_ca(ipaddress.IPv4Network("10.77.0.0/24"), "a" * 32).cert_pem)

    forgejo.Forgejo(cafile).post("/users/x/tokens", {})

    assert {"minted-0123456789", "reg-0123456789"} <= trace._known


# --- the trace file ------------------------------------------------------------------


def test_records_are_private_json_lines_with_trace_and_fields(traced):
    trace.secret("hunter2-hunter2")
    trace.logger("hitkit.test").info("hello", machine="forgejo", detail="pw=hunter2-hunter2")

    (rec,) = records(traced)
    assert rec["msg"] == "hello" and rec["machine"] == "forgejo"
    assert rec["detail"] == "pw=***"
    assert rec["trace"] == trace.trace_id() and rec["cmd"] == "test cmd"
    logs = trace.log_dir(traced)
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    assert stat.S_IMODE(next(logs.iterdir()).stat().st_mode) == 0o600


def test_debug_detail_is_recorded_by_default(traced):
    trace.logger("hitkit.test").debug("detail")

    assert [r["level"] for r in records(traced)] == ["debug"]


def test_trace_id_is_inherited_from_the_environment(user, monkeypatch):
    monkeypatch.setenv("HITKIT_TRACE_ID", "ci-job-42")

    assert trace.setup(user, "x") == "ci-job-42"


def test_old_log_files_are_pruned(user):
    logs = trace.log_dir(user)
    logs.mkdir(parents=True)
    old = logs / f"{(date.today() - timedelta(days=trace.RETENTION_DAYS + 1)).isoformat()}.jsonl"
    recent = logs / f"{(date.today() - timedelta(days=1)).isoformat()}.jsonl"
    old.write_text("")
    recent.write_text("")

    trace.setup(user, "x")

    assert not old.exists() and recent.exists()


def test_an_unwritable_log_dir_never_breaks_hitkit(user):
    user.state_dir.parent.mkdir(parents=True)
    user.state_dir.write_text("not a directory")

    trace.setup(user, "x")
    trace.logger("hitkit.test").error("still fine")


# --- spans and commands ------------------------------------------------------------------


def test_span_records_duration_and_extra_fields(traced):
    with trace.span("machine.up", machine="forgejo") as result:
        result["image"] = "forgejo-abc"

    done = records(traced)[-1]
    assert done["msg"] == "machine.up done" and done["image"] == "forgejo-abc"
    assert done["duration_ms"] >= 0 and done["machine"] == "forgejo"


def test_span_records_failures_and_reraises(traced):
    with pytest.raises(HitkitError):
        with trace.span("machine.up", machine="forgejo"):
            raise HitkitError("boom")

    failed = records(traced)[-1]
    assert failed["level"] == "error" and failed["error"] == "HitkitError: boom"


def test_run_records_argv_and_exit_but_never_stdin(traced):
    trace.run(["sh", "-c", "cat >/dev/null; exit 3"], input="stdin-secret-value", capture_output=True, text=True)

    rec = records(traced)[-1]
    assert rec["msg"] == "command failed" and rec["rc"] == 3
    assert rec["stdin_bytes"] == len("stdin-secret-value")
    assert "stdin-secret-value" not in json.dumps(rec)


def test_run_keeps_the_tail_of_a_failure_and_honours_check(traced):
    with pytest.raises(subprocess.CalledProcessError):
        trace.run(["sh", "-c", "echo broken thing >&2; exit 1"], capture_output=True, text=True, check=True)

    assert records(traced)[-1]["stderr"] == "broken thing"


def test_readiness_failure_records_the_console_tail(traced, tmp_path):
    from hitkit.machine import readiness

    console = tmp_path / "console.log"
    console.write_text("booting\nHITKIT-FAILED gate=network detail=no address\n")

    with pytest.raises(readiness.NotReady):
        readiness.wait(console, 10, lambda: True)

    rec = records(traced)[-1]
    assert rec["msg"] == "machine not ready" and rec["gate"] == "network"
    assert "booting" in rec["console_tail"]


# --- reading traces back ------------------------------------------------------------------


def _write(user, *recs):
    logs = trace.log_dir(user)
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / f"{date.today().isoformat()}.jsonl").open("a") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def test_summaries_and_selection(user):
    _write(user,
           {"trace": "aaa111", "ts": "t1", "cmd": "status", "level": "info", "logger": "hitkit.cli", "msg": "started"},
           {"trace": "aaa111", "ts": "t2", "level": "info", "logger": "hitkit.cli", "exit_code": 0, "duration_ms": 5},
           {"trace": "aab222", "ts": "t3", "cmd": "up forge", "level": "error", "logger": "hitkit.cli",
            "exit_code": 1, "duration_ms": 9})

    rows = trace.summarize(trace.read(user))
    assert [(r["trace"], trace.failed(r)) for r in rows] == [("aaa111", False), ("aab222", True)]
    assert trace.select(user, "last")[0]["trace"] == "aab222"
    assert len(trace.select(user, "aaa1")) == 2
    with pytest.raises(Conflict):
        trace.select(user, "aa")
    with pytest.raises(HitkitError):
        trace.select(user, "zzz")


# --- the CLI ------------------------------------------------------------------------------


@pytest.fixture
def run(user, monkeypatch):
    monkeypatch.setattr(cli, "current_user", lambda: user)
    monkeypatch.delenv("HITKIT_TRACE_ID", raising=False)
    monkeypatch.delenv("HITKIT_DEBUG", raising=False)
    return lambda *args: CliRunner().invoke(cli.cli, list(args))


def test_errors_print_their_trace_id_and_are_recorded(run, user):
    result = run("status")  # not set up in the fixture home

    assert result.exit_code == 1
    tid = trace.trace_id()
    assert f"trace: {tid}" in result.output
    recs = records(user)
    assert recs[0]["msg"] == "command started" and recs[0]["argv"] == ["status"]
    assert recs[-1]["msg"] == "command failed" and recs[-1]["exit_code"] == 1
    assert "not set up" in recs[-1]["error"] and "Traceback" in recs[-1]["exc"]


def test_json_errors_carry_the_trace_id(run):
    out = json.loads(run("--json", "status").stdout)

    assert out["trace"] == trace.trace_id() and "not set up" in out["error"]


def test_unexpected_exceptions_become_a_short_error_with_the_traceback_in_the_trace(run, user, monkeypatch):
    def broken():
        raise KeyError("oops")

    monkeypatch.setattr(cli, "_host_context", broken)

    result = run("status")

    assert result.exit_code == 1
    assert "internal error: KeyError" in result.output and "Traceback" not in result.output
    assert "KeyError: 'oops'" in records(user)[-1]["exc"]


def test_trace_command_lists_and_shows_without_tracing_itself(run, user):
    run("status")
    tid = trace.trace_id()

    listing = run("trace")
    shown = run("trace", "--failed", "last")

    assert tid in listing.output and "failed" in listing.output
    assert f"trace {tid}: hitkit status" in shown.output and "command failed" in shown.output
    assert {r["trace"] for r in records(user)} == {tid}


def test_debug_flag_mirrors_records_to_stderr(run):
    result = run("--debug", "status")

    assert "command started" in result.stderr


@pytest.mark.parametrize("args,expected", [
    (["status", "--json"], ["--json", "status"]),
    (["creds", "forge", "--json", "--debug"], ["--json", "--debug", "creds", "forge"]),
    (["--json", "status"], ["--json", "status"]),
    (["ssh", "devbox", "ls", "--json"], ["ssh", "devbox", "ls", "--json"]),
    (["--json", "ssh", "devbox", "--", "x", "--json"], ["--json", "ssh", "devbox", "--", "x", "--json"]),
    (["up", "forge", "--", "--json"], ["up", "forge", "--", "--json"]),
])
def test_global_flags_are_accepted_after_the_command(args, expected):
    assert cli._hoist_global_flags(args) == expected


def test_json_after_the_command_works(run):
    out = json.loads(run("status", "--json").stdout)

    assert "not set up" in out["error"]
