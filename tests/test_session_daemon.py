"""The libvirt session daemon runs in hitkit's own unit, and stopping it never kills VMs."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from hitkit.host import session


def test_the_unit_waits_for_readiness_and_protects_vms_on_stop():
    text = session.unit_text("/usr/sbin/virtqemud")

    assert "Type=notify" in text  # clients never race it and spawn a second daemon
    assert "KillMode=mixed" in text
    assert f"ExecStop={sys.executable} -m hitkit.host.session stop" in text


def test_stop_all_shuts_down_gracefully_and_saves_stragglers(monkeypatch):
    class Dom:
        def __init__(self, stubborn):
            self.active, self.stubborn, self.saved = True, stubborn, False

        def isActive(self):
            return self.active

        def shutdown(self):
            if not self.stubborn:
                self.active = False

        def managedSave(self, flags):
            self.saved, self.active = True, False

    polite, stubborn = Dom(False), Dom(True)
    conn = SimpleNamespace(listAllDomains=lambda: [polite, stubborn], close=lambda: None)
    fake = SimpleNamespace(open=lambda uri: conn, registerErrorHandler=lambda *a: None, libvirtError=Exception)
    monkeypatch.setitem(sys.modules, "libvirt", fake)

    session.stop_all(timeout=0.1)

    assert not polite.saved and not polite.active
    assert stubborn.saved  # never killed: its state is kept for `up` to resume
