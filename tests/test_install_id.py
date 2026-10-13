"""The install ID is recovered rather than minted again while any copy survives (design §13.13)."""

from __future__ import annotations

import pytest

from hitkit import config
from hitkit.errors import Conflict
from hitkit.host import doctor

DOMAIN = """<domain type='kvm'><name>hitkit-{machine}</name>
  <metadata><hitkit:machine xmlns:hitkit="urn:hitkit:metadata:1">
    <hitkit:install-id>{iid}</hitkit:install-id><hitkit:kit>forge</hitkit:kit>
    <hitkit:machine>{machine}</hitkit:machine><hitkit:address>10.66.0.10</hitkit:address>
  </hitkit:machine></metadata></domain>"""


def saved_domain(user, machine, iid):
    user.libvirt_session_dir.mkdir(parents=True, exist_ok=True)
    (user.libvirt_session_dir / f"hitkit-{machine}.xml").write_text(DOMAIN.format(machine=machine, iid=iid))


def test_save_keeps_a_second_copy_that_load_recovers_from(user):
    config.save(user, config.HostConfig("10.77.0.0/24", "abc"))
    user.config_file.unlink()

    assert config.load(user) == config.HostConfig("10.77.0.0/24", "abc")
    assert user.config_file.exists()  # re-saved


def test_install_id_and_subnet_are_recovered_from_saved_domains(user):
    saved_domain(user, "forgejo", "old-id")
    saved_domain(user, "runner-1", "old-id")

    cfg = config.load_or_create(user)

    assert cfg == config.HostConfig("10.66.0.0/24", "old-id")


def test_a_new_id_is_minted_only_without_any_hitkit_machines(user):
    (user.libvirt_session_dir).mkdir(parents=True)
    (user.libvirt_session_dir / "other-vm.xml").write_text("<domain><name>other</name></domain>")

    assert len(config.load_or_create(user).install_id) == 32


def test_several_install_ids_are_a_conflict_until_the_user_picks_one(user):
    saved_domain(user, "forgejo", "id-a")
    saved_domain(user, "devbox", "id-b")

    with pytest.raises(Conflict, match="several install IDs"):
        config.load_or_create(user)
    assert config.load_or_create(user, install_id="id-b").install_id == "id-b"


def test_install_id_cannot_replace_a_configured_one(user):
    config.save(user, config.HostConfig("10.77.0.0/24", "abc"))

    with pytest.raises(Conflict):
        config.load_or_create(user, install_id="other")


def test_dry_run_recovery_does_not_write(user):
    saved_domain(user, "forgejo", "old-id")

    assert config.load(user, persist=False).install_id == "old-id"
    assert not user.config_file.exists()


def test_doctor_warns_about_machines_of_another_install(user):
    saved_domain(user, "forgejo", "mine")
    saved_domain(user, "devbox", "theirs")

    result = doctor.check_install_id(user, config.HostConfig("10.77.0.0/24", "mine"))

    assert result.status == "warn" and "devbox" in result.message
