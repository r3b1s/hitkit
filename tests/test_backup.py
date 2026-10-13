"""Backups and restores (design §13.13): layout, integrity, retention, and never losing current data."""

from __future__ import annotations

import json
import tarfile
from types import SimpleNamespace

import pytest

from hitkit import backup, kits, secrets
from hitkit.errors import HitkitError
from hitkit.machine.metadata import MachineMeta, Ownership


def test_forge_backs_up_only_forgejo_online_with_forgejo_stopped():
    hooks = kits.hooks("forge")

    assert hooks.backup_plan(SimpleNamespace(name="forgejo")) == backup.BackupPlan("online", ("forgejo.service",))
    assert hooks.backup_plan(SimpleNamespace(name="runner-1")) is None


def test_other_kits_default_to_an_offline_copy():
    assert kits.hooks("proxmox").backup_plan(SimpleNamespace(name="proxmox")) == backup.BackupPlan()


def test_online_commands_restart_services_even_on_failure_and_never_delete():
    plan = backup.BackupPlan("online", ("forgejo.service",))

    out = backup._online_backup_command(plan, "zstd -T0 -3")
    back = backup._online_restore_command(plan, "zstd", "20260101T000000Z")

    assert "trap 'systemctl start forgejo.service' EXIT" in out and "systemctl stop forgejo.service" in out
    assert "--exclude='./.hitkit-pre-restore-*'" in out
    assert "rm " not in back and "-delete" not in back
    assert "mv -t /srv/data/.hitkit-pre-restore-20260101T000000Z" in back


# --- create / list / prune / verify with a faked SSH stream ------------------------------------


@pytest.fixture
def forge(user, monkeypatch, tmp_path):
    secrets.put(user, "forge", "forgejo.secret-key", "s3cret-key-value")
    meta = MachineMeta("i", "forge", 1, "forgejo", "forgejo", "forgejo-abc", "10.77.0.10", "2026-01-01T00:00:00+00:00")
    infos = [SimpleNamespace(name="forgejo", ownership=Ownership.OWNED, meta=meta)]
    from hitkit.machine import lifecycle, remote

    monkeypatch.setattr(lifecycle, "kit_machines", lambda conn, iid, kit: infos)
    monkeypatch.setattr(remote, "run", lambda hctx, m, cmd, stdin=None: "HITKIT-READY\n" if "ready" in cmd else "/usr/bin/zstd\n")
    monkeypatch.setattr(remote, "ssh_args", lambda hctx, m: ["ssh", m])
    streamed = []

    def fake_stream_out(argv, dest):
        streamed.append(argv)
        dest.write_bytes(b"tarball")
        return 7, backup.hashlib.sha256(b"tarball").hexdigest()

    monkeypatch.setattr(backup, "_stream_out", fake_stream_out)
    hctx = SimpleNamespace(user=user, cfg=SimpleNamespace(install_id="i"))
    return SimpleNamespace(hctx=hctx, kit=kits.load("forge"), hooks=kits.hooks("forge"), streamed=streamed)


def make(forge, keep=7):
    return backup.create(None, forge.hctx, forge.kit, forge.hooks, None, keep, lambda m: None)


def test_a_backup_holds_data_secrets_and_a_manifest(forge, user):
    path = make(forge)

    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["machines"]["forgejo"]["method"] == "online"
    assert (path / "forgejo.data.tar.zst").read_bytes() == b"tarball"
    with tarfile.open(path / "secrets.tar") as tar:
        assert "forge/forgejo.secret-key" in tar.getnames()
    assert oct((path / "secrets.tar").stat().st_mode & 0o777) == "0o600"
    assert backup.verify(path)["kit"] == "forge"


def test_corruption_is_detected_before_restoring(forge):
    path = make(forge)
    (path / "forgejo.data.tar.zst").write_bytes(b"tampered")

    with pytest.raises(HitkitError, match="corrupt"):
        backup.verify(path)


def test_only_the_newest_backups_are_kept(forge, user, monkeypatch):
    stamps = iter(f"20260101T00000{i // 2}Z" for i in range(6))  # create() asks the time twice

    class Clock(backup.dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return backup.dt.datetime.strptime(next(stamps), "%Y%m%dT%H%M%SZ").replace(tzinfo=tz)

    monkeypatch.setattr(backup.dt, "datetime", Clock)
    for _ in range(3):
        make(forge, keep=2)

    assert [b.path.name for b in backup.list_backups(user, "forge")] == ["20260101T000001Z", "20260101T000002Z"]


def test_restoring_secrets_moves_the_current_ones_aside(forge, user):
    path = make(forge)
    secrets.put(user, "forge", "forgejo.secret-key", "changed-since-backup")

    aside = backup._restore_secrets(user, "forge", path / "secrets.tar", "20260101T000000Z")

    assert secrets.get(user, "forge", "forgejo.secret-key") == "s3cret-key-value"
    assert (aside / "forgejo.secret-key").read_text() == "changed-since-backup"


def test_age_notice_warns_without_backups(user):
    assert "has no backup" in backup.age_notice(user, "forge")
