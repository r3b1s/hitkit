"""Pure-logic tests for `hitkit doctor`: no libvirt and no system access."""

from __future__ import annotations

import ipaddress
from pathlib import Path

from hitkit.host import doctor
from hitkit.host.doctor import CheckResult
from hitkit.paths import UserContext

SUBNET = ipaddress.IPv4Network("10.77.0.0/24")


def test_overlapping_route_on_other_device_conflicts():
    routes = [{"dst": "10.77.0.0/16", "dev": "wlp1s0"}]
    assert len(doctor.find_route_conflicts(routes, SUBNET, "hitkit0")) == 1


def test_route_on_own_device_is_ignored():
    routes = [{"dst": "10.77.0.0/24", "dev": "hitkit0"}]
    assert doctor.find_route_conflicts(routes, SUBNET, "hitkit0") == []


def test_default_route_is_ignored():
    routes = [{"dst": "default", "dev": "wlp1s0"}]
    assert doctor.find_route_conflicts(routes, SUBNET, "hitkit0") == []


def test_bare_host_ip_inside_subnet_conflicts():
    routes = [{"dst": "10.77.0.5", "dev": "tun0"}]
    assert len(doctor.find_route_conflicts(routes, SUBNET, "hitkit0")) == 1


def test_non_overlapping_routes_do_not_conflict():
    routes = [{"dst": "192.168.1.0/24", "dev": "eth0"}, {"dst": "10.78.0.0/24", "dev": "tun0"}]
    assert doctor.find_route_conflicts(routes, SUBNET, "hitkit0") == []


def _boom() -> CheckResult:
    raise RuntimeError("boom")


def test_run_checks_turns_exceptions_into_fail(monkeypatch):
    user = UserContext("t", 1000, 1000, Path("/home/t"))
    for attr in dir(doctor):
        if attr.startswith("check_"):
            monkeypatch.setattr(doctor, attr, lambda *args, _n=attr: CheckResult(_n, "ok", "stub"))
    monkeypatch.setattr(doctor, "check_kvm", _boom)

    results = doctor.run_checks(user, None)

    assert results[0].name == "kvm"
    assert results[0].status == "fail"
    assert "boom" in results[0].message
    assert all(r.status == "ok" for r in results[1:])
