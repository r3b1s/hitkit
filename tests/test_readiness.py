"""Guest readiness verdicts read from the serial console (design §8.6)."""

from __future__ import annotations

import pytest

from hitkit.machine.readiness import Failed, NotReady, Ready, scan, wait


class FakeClock:
    """A clock that only advances when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake() -> FakeClock:
    return FakeClock()


def test_scan_without_markers_is_none():
    assert scan("[    1.0] systemd[1]: Started something.\n") is None


def test_scan_ready():
    assert scan("HITKIT-READY\n") == Ready()


def test_scan_ready_with_carriage_return_and_surrounding_kernel_lines():
    text = "[  2.1] kernel: eth0 up\r\nHITKIT-READY\r\n[  2.2] login: \r\n"
    assert scan(text) == Ready()


def test_scan_failed_parses_gate_and_detail():
    text = "HITKIT-FAILED gate=network detail=address 10.77.0.40 not configured\n"
    assert scan(text) == Failed("network", "address 10.77.0.40 not configured")


def test_last_marker_wins_failed_then_ready():
    text = "HITKIT-FAILED gate=ssh detail=sshd down\nHITKIT-READY\n"
    assert scan(text) == Ready()


def test_last_marker_wins_ready_then_failed():
    text = "HITKIT-READY\nHITKIT-FAILED gate=data detail=disk missing\n"
    assert scan(text) == Failed("data", "disk missing")


def test_wait_returns_when_console_reports_ready(tmp_path, fake):
    log = tmp_path / "console.log"
    log.write_text("boot...\nHITKIT-READY\n")
    wait(log, timeout=60, is_running=lambda: True, clock=fake.clock, sleep=fake.sleep)
    assert fake.sleeps == []


def test_wait_raises_on_failed_marker(tmp_path, fake):
    log = tmp_path / "console.log"
    log.write_text("HITKIT-FAILED gate=network detail=no link\n")
    with pytest.raises(NotReady, match="network"):
        wait(log, timeout=60, is_running=lambda: True, clock=fake.clock, sleep=fake.sleep)


def test_wait_raises_when_machine_stops(tmp_path, fake):
    log = tmp_path / "console.log"
    log.write_text("boot...\n")
    with pytest.raises(NotReady, match="stopped"):
        wait(log, timeout=60, is_running=lambda: False, clock=fake.clock, sleep=fake.sleep)


def test_wait_raises_on_timeout(tmp_path, fake):
    log = tmp_path / "console.log"
    log.write_text("boot...\n")
    with pytest.raises(NotReady, match="no readiness report"):
        wait(log, timeout=5, is_running=lambda: True, poll=1.0, clock=fake.clock, sleep=fake.sleep)
    assert fake.now >= 5


def test_wait_tolerates_missing_console_log_until_it_appears(tmp_path, fake):
    log = tmp_path / "console.log"
    polls = []

    def fake_sleep(seconds: float) -> None:
        fake.sleep(seconds)
        polls.append(seconds)
        if len(polls) == 3:  # the guest starts writing after a few polls
            log.write_text("HITKIT-READY\n")

    wait(log, timeout=60, is_running=lambda: True, poll=1.0, clock=fake.clock, sleep=fake_sleep)
    assert len(polls) == 3


def test_marker_glued_to_login_prompt_is_found():
    # getty prints "cache login: " without a newline; the marker lands on that line
    text = "Debian GNU/Linux 13 cache ttyS0\n\ncache login: HITKIT-READY\n"
    assert isinstance(scan(text), Ready)
    failed = scan("cache login: HITKIT-FAILED gate=dns detail=nope\n")
    assert isinstance(failed, Failed) and failed.gate == "dns"


def test_ready_inside_other_words_is_not_a_marker():
    assert scan("NOT-HITKIT-READYISH\n") is None
