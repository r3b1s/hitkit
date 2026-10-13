"""Proxmox images leave apt repositories as Proxmox's UI does without a subscription."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROLE = Path(__file__).resolve().parents[1] / "src" / "hitkit" / "images" / "proxmox"
HELPER = ROLE / "mkosi.extra" / "usr" / "lib" / "hitkit" / "pve-repos"
ENTERPRISE = ("Types: deb\nURIs: https://enterprise.proxmox.com/debian/pve\nSuites: trixie\n"
              "Components: pve-enterprise\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n")


def run(tmp_path: Path) -> Path:
    d = tmp_path / "sources.list.d"
    d.mkdir(exist_ok=True)
    script = tmp_path / "pve-repos"
    script.write_text(HELPER.read_text().replace("d=/etc/apt/sources.list.d", f"d={d}"))
    subprocess.run(["sh", str(script)], check=True)
    return d


def test_enterprise_entries_are_kept_but_disabled(tmp_path):
    d = tmp_path / "sources.list.d"
    d.mkdir()
    (d / "pve-enterprise.sources").write_text(ENTERPRISE)

    run(tmp_path)
    run(tmp_path)  # idempotent

    text = (d / "pve-enterprise.sources").read_text()
    assert "pve-enterprise" in text and text.count("Enabled: false") == 1


def test_a_deleted_enterprise_entry_is_recreated_disabled(tmp_path):
    # Ludus's Ansible deletes it; Proxmox's UI can only re-enable what exists.
    d = run(tmp_path)

    assert "Enabled: false" in (d / "pve-enterprise.sources").read_text()


def test_explicit_choices_are_left_alone(tmp_path):
    d = tmp_path / "sources.list.d"
    d.mkdir()
    (d / "ceph.sources").write_text(
        "Types: deb\nURIs: https://enterprise.proxmox.com/debian/ceph-squid\nSuites: trixie\nComponents: enterprise\n\n"
        "Types: deb\nURIs: http://download.proxmox.com/debian/ceph-squid\nSuites: trixie\n"
        "Components: no-subscription\nEnabled: true\n")

    run(tmp_path)

    stanzas = (d / "ceph.sources").read_text().strip().split("\n\n")
    assert "Enabled: false" in stanzas[0] and "Enabled: true" in stanzas[1]


def test_images_ship_debian_and_community_sources_and_never_delete_enterprise():
    extra = ROLE / "mkosi.extra" / "etc" / "apt" / "sources.list.d"
    assert "trixie-security" in (extra / "debian.sources").read_text()
    assert "pve-no-subscription" in (extra / "proxmox.sources").read_text()
    postinst = (ROLE / "mkosi.postinst.chroot").read_text()
    assert "/usr/lib/hitkit/pve-repos" in postinst and "rm -f /etc/apt/sources.list.d/pve-enterprise" not in postinst
    ludus = (ROLE.parent / "ludus" / "mkosi.extra" / "usr" / "lib" / "hitkit" / "ludus-install").read_text()
    assert "/usr/lib/hitkit/pve-repos" in ludus
