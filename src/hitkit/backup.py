"""Backups and restores of a kit's data (design §13.13).

A backup is a directory <dest>/<kit>/<UTC timestamp>/ holding, per machine, its
data (everything on /srv/data), plus the kit's secrets and a manifest with
sha256 sums. Two methods:

- online: stream `tar` of /srv/data over SSH while the machine runs, with the
  kit's writing services stopped for the duration (forge: Forgejo, so the
  repositories and the SQLite database are consistent; downtime is seconds).
- offline: stop the machine, copy its data disk block for block (compressed
  qcow2), start it again. Exact, works for anything (Proxmox guests included),
  costs downtime for the whole copy.

Kits choose per machine through `Hooks.backup_plan` (default: offline; None
skips machines with nothing unique, e.g. CI runners).

A restore never deletes current data: online restores move the current
/srv/data contents aside inside the machine, offline restores keep the current
data disk in vms/.kept/, and the current secrets are moved aside on the host.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from hitkit import __version__, secrets, trace
from hitkit.errors import HitkitError
from hitkit.paths import UserContext, ensure_private_dir

_t = trace.logger(__name__)

DEFAULT_KEEP = 7
DATA = "/srv/data"
PRE_RESTORE = ".hitkit-pre-restore-"
_STAMP = re.compile(r"^\d{8}T\d{6}Z$")
_CHUNK = 1 << 20


@dataclass(frozen=True)
class BackupPlan:
    method: str = "offline"  # "online" | "offline"
    quiesce: tuple[str, ...] = ()  # systemd units stopped during an online backup or restore


@dataclass
class Settings:
    """Per-kit backup settings, remembered from the last explicit `hitkit backup` (secrets/<kit>/backup.json)."""
    dest: str | None = None
    keep: int = DEFAULT_KEEP
    schedule: str = "off"

    @classmethod
    def load(cls, user: UserContext, kit: str) -> "Settings":
        raw = secrets.get(user, kit, "backup.json")
        return cls(**json.loads(raw)) if raw else cls()

    def save(self, user: UserContext, kit: str) -> None:
        secrets.put(user, kit, "backup.json", json.dumps(self.__dict__))


def root_dir(user: UserContext, dest: str | None) -> Path:
    return Path(dest).expanduser() if dest else user.data_dir / "backups"


def kit_dir(user: UserContext, kit: str, dest: str | None = None) -> Path:
    return root_dir(user, dest) / kit


@dataclass
class BackupInfo:
    path: Path
    kit: str
    created: str
    machines: dict = field(default_factory=dict)
    size_bytes: int = 0


def list_backups(user: UserContext, kit: str, dest: str | None = None) -> list[BackupInfo]:
    """Complete backups (those with a manifest), oldest first."""
    directory = kit_dir(user, kit, dest)
    found = []
    for path in sorted(directory.glob("*")) if directory.is_dir() else []:
        manifest = path / "manifest.json"
        if not (_STAMP.match(path.name) and manifest.is_file()):
            continue
        data = json.loads(manifest.read_text())
        size = sum(f["size"] for m in data["machines"].values() for f in (m["file"], m.get("os_file"), m.get("state_file")) if f)
        found.append(BackupInfo(path, data["kit"], data["created"], data["machines"], size))
    return found


def age_notice(user: UserContext, kit: str) -> str:
    """One line for `destroy --delete-data`: how old the newest backup is, or that there is none."""
    settings = Settings.load(user, kit)
    backups = list_backups(user, kit, settings.dest)
    if not backups:
        return f"warning: {kit} has no backup; its data will be gone for good (hitkit backup {kit})"
    newest = backups[-1]
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(newest.created)
    return f"newest backup of {kit}: {_human_age(age)} old ({newest.path})"


def _human_age(delta: dt.timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} days"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


# --- streaming over SSH -----------------------------------------------------------------


def _stream_out(argv: list[str], dest: Path) -> tuple[int, str]:
    """Run argv, writing its stdout to dest. Returns (size, sha256)."""
    h, size = hashlib.sha256(), 0
    started = time.monotonic()
    with dest.open("wb") as out, subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        while chunk := proc.stdout.read(_CHUNK):
            out.write(chunk)
            h.update(chunk)
            size += len(chunk)
        err = proc.stderr.read().decode(errors="replace").strip()
        rc = proc.wait()
    _t.info("streamed from machine", argv=argv, rc=rc, bytes=size,
            duration_ms=round((time.monotonic() - started) * 1000), stderr=err[-2000:] or None)
    if rc != 0:
        raise HitkitError(f"backup stream failed: {err or f'exit {rc}'}")
    return size, h.hexdigest()


def _stream_in(argv: list[str], src: Path) -> None:
    with src.open("rb") as fh:
        result = trace.run(argv, stdin=fh, capture_output=True, text=True)
    if result.returncode != 0:
        raise HitkitError(f"restore stream failed: {result.stderr.strip() or f'exit {result.returncode}'}")


def _healthy_again(hctx, name: str, remote, log) -> None:
    """Re-run the machine's readiness gates (services were stopped and started)."""
    log(f"waiting for {name}'s services to be ready again")
    try:  # the unit re-runs every gate with the machine's credentials; fails if one never passes
        remote.run(hctx, name, "systemctl restart hitkit-ready.service")
    except HitkitError as e:
        raise HitkitError(f"{name} is not healthy after the copy: {e}",
                          hint=f"hitkit ssh {name} -- journalctl -u hitkit-ready -b --no-pager") from None


def _units(plan: BackupPlan) -> str:
    return " ".join(plan.quiesce)


def _online_backup_command(plan: BackupPlan, compressor: str) -> str:
    # Services are restarted by the EXIT trap even if tar or the connection fails.
    stop = f"systemctl stop {_units(plan)}; trap 'systemctl start {_units(plan)}' EXIT; " if plan.quiesce else ""
    return (f"set -e; {stop}tar --numeric-owner --xattrs --acls --exclude='./{PRE_RESTORE}*' "
            f"-I '{compressor}' -cf - -C {DATA} .")


def _online_restore_command(plan: BackupPlan, compressor: str, stamp: str) -> str:
    aside = f"{DATA}/{PRE_RESTORE}{stamp}"
    stop = f"systemctl stop {_units(plan)}; trap 'systemctl start {_units(plan)}' EXIT; " if plan.quiesce else ""
    return (f"set -e; {stop}mkdir {aside}; "
            f"find {DATA} -mindepth 1 -maxdepth 1 ! -name '{PRE_RESTORE}*' ! -name lost+found "
            f"-exec mv -t {aside} {{}} +; "
            f"tar --numeric-owner --xattrs --acls -I '{compressor}' -xpf - -C {DATA}")


# --- backup -------------------------------------------------------------------------------


def create(conn, hctx, kit, hooks, dest: str | None, keep: int, log: Callable[[str], None],
           method: str | None = None) -> Path:
    """Back up every machine of the kit that has data worth keeping; returns the backup directory."""
    from hitkit.machine import images, lifecycle, remote

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = kit_dir(hctx.user, kit.name, dest)
    ensure_private_dir(directory.parent, hctx.user)
    ensure_private_dir(directory, hctx.user)
    partial = directory / f".{stamp}.partial"
    ensure_private_dir(partial, hctx.user)
    manifest = {"kit": kit.name, "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "hitkit": __version__, "machines": {}}
    infos = {i.name: i for i in lifecycle.kit_machines(conn, hctx.cfg.install_id, kit.name)}
    with trace.span("backup.create", kit=kit.name, dest=directory) as result:
        try:
            for name, info in sorted(infos.items()):
                plan = hooks.backup_plan(info)
                if plan is None:
                    continue
                spec = kit.spec_for(name)
                stateful = bool(spec and spec.stateful_os)
                how = "offline" if stateful else method or plan.method  # system state is on the OS disk
                if how == "online":
                    entry = _backup_online(hctx, name, plan, partial, log, remote)
                else:
                    # An installer machine has a single disk (its data disk) holding the whole system.
                    include_os = stateful and not images.is_installer_role(spec.role)
                    entry = _backup_offline(conn, hctx, name, partial, log, lifecycle, include_os,
                                            _probe(hooks, hctx, spec))
                entry.update(image=info.meta.image if info.meta else None,
                             versions=info.meta.versions if info.meta else {}, quiesce=list(plan.quiesce))
                manifest["machines"][name] = entry
            if not manifest["machines"]:
                raise HitkitError(f"kit {kit.name!r} has no machines to back up", hint=f"hitkit status {kit.name}")
            secrets_tar = partial / "secrets.tar"
            with tarfile.open(secrets_tar, "w") as tar:
                if secrets.kit_dir(hctx.user, kit.name).is_dir():  # devbox and others may have none
                    tar.add(secrets.kit_dir(hctx.user, kit.name), arcname=kit.name)
            secrets_tar.chmod(0o600)
            manifest["secrets"] = {"name": secrets_tar.name, "sha256": _sha256(secrets_tar)}
            (partial / "manifest.json").write_text(json.dumps(manifest, indent=2))
            final = directory / stamp
            partial.rename(final)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        removed = prune(hctx.user, kit.name, dest, keep)
        result.update(path=final, machines=sorted(manifest["machines"]), pruned=len(removed))
    return final


def _backup_online(hctx, name: str, plan: BackupPlan, partial: Path, log, remote) -> dict:
    zstd = remote.run(hctx, name, "command -v zstd || true").strip()
    compressor, suffix = ("zstd -T0 -3", "tar.zst") if zstd else ("gzip -1", "tar.gz")
    dest = partial / f"{name}.data.{suffix}"
    units = f" (stopping {_units(plan)} meanwhile)" if plan.quiesce else ""
    log(f"backing up {name}:{DATA} online{units}")
    size, digest = _stream_out(remote.ssh_args(hctx, name) + [_online_backup_command(plan, compressor)], dest)
    _healthy_again(hctx, name, remote, log)
    return {"method": "online", "file": {"name": dest.name, "size": size, "sha256": digest},
            "compressor": compressor.split()[0]}


def _probe(hooks, hctx, spec):
    """The readiness probe an installer machine needs to be started again (None for others)."""
    from hitkit import kits

    return kits.installer_hooks(hooks, hctx, spec, (spec,)).get("probe") if spec else None


def _backup_offline(conn, hctx, name: str, partial: Path, log, lifecycle, include_os: bool = False,
                    probe=None) -> dict:
    from hitkit.machine import disks

    found = lifecycle.get(conn, name, hctx.cfg.install_id)
    was_running = bool(found and (found[0].isActive() or lifecycle.has_saved_state(found[0])))
    what = "OS and data disks" if include_os else "data disk"
    log(f"backing up {name}'s {what} offline (the machine is stopped for the copy)")
    if was_running:
        lifecycle.stop(conn, hctx, name)
    dest = partial / f"{name}.data.qcow2"
    os_dest = partial / f"{name}.os.qcow2"
    state_dest = partial / f"{name}.data.state.tar"
    has_state = hctx.paths(name).tpm_dir.is_dir()  # an installer machine's disk key is sealed to it
    try:
        disks.compressed_copy(hctx.paths(name).data_disk, dest)
        if include_os:  # flattened: the copy does not depend on a cached base image
            disks.compressed_copy(hctx.paths(name).os_disk, os_dest)
        if has_state:
            with tarfile.open(state_dest, "w") as tar:
                if hctx.paths(name).nvram.exists():
                    tar.add(hctx.paths(name).nvram, arcname="nvram.fd")
                if hctx.paths(name).tpm_dir.exists():
                    tar.add(hctx.paths(name).tpm_dir, arcname="tpm")
            state_dest.chmod(0o600)
    finally:
        if was_running:
            lifecycle.start(conn, hctx, name, log, probe)
    entry = {"method": "offline", "file": {"name": dest.name, "size": dest.stat().st_size, "sha256": _sha256(dest)}}
    if include_os:
        entry["os_file"] = {"name": os_dest.name, "size": os_dest.stat().st_size, "sha256": _sha256(os_dest)}
    if has_state and state_dest.exists():
        entry["state_file"] = {"name": state_dest.name, "size": state_dest.stat().st_size, "sha256": _sha256(state_dest)}
    return entry


def prune(user: UserContext, kit: str, dest: str | None, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` complete backups (and stale partial ones)."""
    backups = list_backups(user, kit, dest)
    doomed = [b.path for b in backups[:-keep]] if keep > 0 else []
    directory = kit_dir(user, kit, dest)
    doomed += [p for p in directory.glob(".*.partial")] if directory.is_dir() else []
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
        _t.info("backup pruned", path=path)
    return doomed


# --- restore -------------------------------------------------------------------------------


def verify(backup: Path) -> dict:
    manifest_path = backup / "manifest.json"
    if not manifest_path.is_file():
        raise HitkitError(f"{backup} is not a hitkit backup (no manifest.json)", hint="hitkit backup --list")
    manifest = json.loads(manifest_path.read_text())
    files = [f for m in manifest["machines"].values() for f in (m["file"], m.get("os_file"), m.get("state_file")) if f]
    files.append(manifest["secrets"])
    for f in files:
        path = backup / f["name"]
        if not path.is_file() or _sha256(path) != f["sha256"]:
            raise HitkitError(f"backup file {path} is missing or corrupt (sha256 mismatch)")
    return manifest


def restore(conn, hctx, kit, hooks, backup: Path, log: Callable[[str], None]) -> dict:
    """Put a backup's data and secrets back. The kit's wiring (`up`) must run afterwards."""
    from hitkit.machine import lifecycle, remote

    manifest = verify(backup)
    if manifest["kit"] != kit.name:
        raise HitkitError(f"{backup} is a backup of {manifest['kit']!r}, not {kit.name!r}")
    infos = {i.name: i for i in lifecycle.kit_machines(conn, hctx.cfg.install_id, kit.name)}
    missing = sorted(set(manifest["machines"]) - set(infos))
    if missing:
        raise HitkitError(f"machines {', '.join(missing)} do not exist", hint=f"hitkit up {kit.name}, then restore")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    moved: dict[str, str] = {}
    with trace.span("backup.restore", kit=kit.name, backup=backup):
        moved["secrets"] = str(_restore_secrets(hctx.user, kit.name, backup / manifest["secrets"]["name"], stamp))
        for name, entry in sorted(manifest["machines"].items()):
            src = backup / entry["file"]["name"]
            if entry["method"] == "online":
                plan = BackupPlan("online", tuple(entry.get("quiesce", ())))
                compressor = "zstd" if entry.get("compressor") == "zstd" else "gzip"  # tar adds -d
                log(f"restoring {name}:{DATA} (current contents move to {DATA}/{PRE_RESTORE}{stamp})")
                _stream_in(remote.ssh_args(hctx, name) + [_online_restore_command(plan, compressor, stamp)], src)
                _healthy_again(hctx, name, remote, log)
                moved[name] = f"{name}:{DATA}/{PRE_RESTORE}{stamp}"
            else:
                os_src = backup / entry["os_file"]["name"] if entry.get("os_file") else None
                state_src = backup / entry["state_file"]["name"] if entry.get("state_file") else None
                what = "OS and data disks" if os_src else "data disk"
                log(f"restoring {name}'s {what} (the current ones are kept in vms/.kept)")
                kept = _restore_offline(conn, hctx, name, src, log, lifecycle, os_src, state_src,
                                        _probe(hooks, hctx, kit.spec_for(name)))
                moved[name] = ", ".join(str(k) for k in kept)
    return moved


def _restore_secrets(user: UserContext, kit: str, tar_path: Path, stamp: str) -> Path | None:
    current = secrets.kit_dir(user, kit)
    aside = None
    if current.exists():
        aside = current.parent / ".pre-restore" / f"{kit}-{stamp}"
        ensure_private_dir(aside.parent, user)
        current.rename(aside)
    ensure_private_dir(current.parent, user)
    with tarfile.open(tar_path) as tar:
        members = [m for m in tar.getmembers() if m.name == kit or m.name.startswith(f"{kit}/")]
        tar.extractall(current.parent, members=members, filter="data")
    if current.exists():
        current.chmod(0o700)
    return aside


def _restore_offline(conn, hctx, name: str, src: Path, log, lifecycle, os_src: Path | None = None,
                     state_src: Path | None = None, probe=None) -> list[Path]:
    from hitkit.machine import disks

    found = lifecycle.get(conn, name, hctx.cfg.install_id)
    was_running = bool(found and (found[0].isActive() or lifecycle.has_saved_state(found[0])))
    lifecycle.stop(conn, hctx, name)  # also resolves a saved memory image before the disks change
    kept = lifecycle.keep_disks(hctx, name, "replaced by a restore", stateful_os=os_src is not None)
    disks.expand_copy(src, hctx.paths(name).data_disk)
    if os_src is not None:
        disks.expand_copy(os_src, hctx.paths(name).os_disk)
    if state_src is not None:  # NVRAM and TPM state, for a disk whose key is sealed to them
        with tarfile.open(state_src) as tar:
            tar.extractall(hctx.paths(name).root, filter="data")
    if was_running:
        lifecycle.start(conn, hctx, name, log, probe)
    return kept


# --- schedule ------------------------------------------------------------------------------

UNIT_PREFIX = "hitkit-backup@"


def unit_dir(user: UserContext) -> Path:
    return user.home / ".config" / "systemd" / "user"


def unit_files() -> dict[str, str]:
    import sys

    hitkit = Path(sys.argv[0]).resolve() if Path(sys.argv[0]).name == "hitkit" else shutil.which("hitkit")
    return {
        "hitkit-backup@.service": (
            "[Unit]\nDescription=hitkit backup of kit %i\n\n"
            f"[Service]\nType=oneshot\nExecStart={hitkit} backup %i\n"
        ),
        "hitkit-backup@.timer": (
            "[Unit]\nDescription=Daily hitkit backup of kit %i\n\n"
            "[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"
        ),
    }


def set_schedule(user: UserContext, kit: str, schedule: str) -> None:
    def systemctl(*args: str) -> None:
        trace.run(["systemctl", "--user", *args], check=True, capture_output=True)

    timer = f"{UNIT_PREFIX}{kit}.timer"
    if schedule == "daily":
        directory = unit_dir(user)
        directory.mkdir(parents=True, exist_ok=True)
        for name, text in unit_files().items():
            (directory / name).write_text(text)
        systemctl("daemon-reload")
        systemctl("enable", "--now", timer)
    else:
        trace.run(["systemctl", "--user", "disable", "--now", timer], capture_output=True)
