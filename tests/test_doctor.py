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


# --- swtpm and secure-boot OVMF checks (plan §8, §9) -----------------------


def test_check_swtpm_warns_when_missing_and_ok_when_present(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda *a, **k: None)
    res = doctor.check_swtpm()
    assert res.name == "swtpm"
    assert res.status == "warn"
    assert "incus-os" in res.message

    monkeypatch.setattr(doctor.shutil, "which", lambda *a, **k: "/usr/bin/swtpm")
    monkeypatch.setattr(doctor.hostinfo, "detect", lambda: doctor.hostinfo.Distro("arch", "Arch"))
    res_ok = doctor.check_swtpm()
    assert res_ok.name == "swtpm"
    assert res_ok.status == "ok"


def test_check_secure_boot_ovmf_warns_when_missing_and_ok_when_present(monkeypatch):
    monkeypatch.setattr(doctor.hostinfo, "secure_boot_ovmf_available", lambda: False)
    res = doctor.check_secure_boot_ovmf()
    assert res.name == "secure-boot-ovmf"
    assert res.status == "warn"
    assert "incus-os" in res.message

    monkeypatch.setattr(doctor.hostinfo, "secure_boot_ovmf_available", lambda: True)
    res_ok = doctor.check_secure_boot_ovmf()
    assert res_ok.name == "secure-boot-ovmf"
    assert res_ok.status == "ok"

