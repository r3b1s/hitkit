"""Create, start, stop, and destroy machines in qemu:///session (design §8).

Ownership is always decided from libvirt metadata (never names). The machine
directory is created before any libvirt object and removed last, so a crash
leaves either nothing or a directory `gc` can reclaim.

Data disks are never deleted implicitly (design §13.13): whenever a machine
directory is cleared without an explicit request to delete data, its data disk
is first moved to vms/.kept/, where the next `up` of that machine reuses it.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from hitkit import trace, virt
from hitkit.addressing import slot_address
from hitkit.config import HostConfig
from hitkit.errors import Conflict, HitkitError
from hitkit.host.ca import CA
from hitkit.machine import creds, disks, images, metadata, readiness
from hitkit.machine.domain import domain_xml
from hitkit.machine.metadata import Ownership
from hitkit.machine.spec import DOMAIN_PREFIX, MachinePaths, MachineSpec, mac_address
from hitkit.paths import UserContext, ensure_private_dir

READY_TIMEOUT = 300

# Supplied by the kit layer for installer roles (images/<role>/installer.toml).
Seed = Callable[[], dict[str, dict]]  # seed sections for a fresh install
Probe = Callable[[], str | None]  # None when ready, otherwise why not

_STATES = {0: "unknown", 1: "running", 2: "blocked", 3: "paused", 4: "stopping",
           5: "stopped", 6: "crashed", 7: "suspended"}

_t = trace.logger(__name__)


@dataclass(frozen=True)
class HostContext:
    user: UserContext
    cfg: HostConfig
    ca: CA

    @property
    def vms_dir(self) -> Path:
        return self.user.data_dir / "vms"

    def paths(self, name: str) -> MachinePaths:
        return MachinePaths.for_machine(self.vms_dir, name)


@dataclass(frozen=True)
class MachineInfo:
    name: str
    ownership: Ownership
    meta: metadata.MachineMeta | None
    state: str
    cpus: int | None = None  # as defined (applies from the next boot if changed while running)
    memory_mib: int | None = None


def _libvirt():
    return virt._libvirt()


def _meta_xml(dom) -> str | None:
    lv = _libvirt()
    try:
        return dom.metadata(lv.VIR_DOMAIN_METADATA_ELEMENT, metadata.NS)
    except lv.libvirtError as e:
        if e.get_error_code() == lv.VIR_ERR_NO_DOMAIN_METADATA:
            return None
        raise


def _lookup(conn, domain_name: str):
    lv = _libvirt()
    try:
        return conn.lookupByName(domain_name)
    except lv.libvirtError as e:
        if e.get_error_code() == lv.VIR_ERR_NO_DOMAIN:
            return None
        raise


def _info(dom, install_id: str) -> MachineInfo:
    ownership, meta = metadata.classify(_meta_xml(dom), install_id)
    name = meta.machine if meta else dom.name().removeprefix(DOMAIN_PREFIX)
    cpus, memory_mib = _defined_size(dom)
    return MachineInfo(name, ownership, meta, _STATES.get(dom.state()[0], "unknown"), cpus, memory_mib)


def _defined_size(dom) -> tuple[int | None, int | None]:
    """vCPUs and memory (MiB) of the persistent definition, i.e. what the next boot gets."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(dom.XMLDesc(_libvirt().VIR_DOMAIN_XML_INACTIVE))
        units = {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "b": 1 / 2**20, "bytes": 1 / 2**20}
        mem = root.find("memory")
        memory_mib = round(int(mem.text) * units.get(mem.get("unit", "KiB"), 1 / 1024))
        return int(root.findtext("vcpu")), memory_mib
    except Exception:  # sizing is informational here; never fail a listing over it
        return None, None


DATA_DEVICE = "vdb"  # the data disk's target in domain.py


def has_install_media(dom) -> bool:
    """An installer machine still has its install media attached: its install has not finished
    (or `up` was interrupted before detaching it)."""
    return "install.qcow2" in dom.XMLDesc(0)


def detach_install_media(dom) -> None:
    """Detach the install and seed disks (domain.py's sdb, sdc), live and from the definition.
    IncusOS notices the device going away and reboots into the installed system."""
    lv = _libvirt()
    flags = lv.VIR_DOMAIN_AFFECT_LIVE | lv.VIR_DOMAIN_AFFECT_CONFIG
    for target in ("sdb", "sdc"):
        xml = f"<disk type='file' device='disk'><target dev='{target}' bus='usb'/></disk>"
        try:
            dom.detachDeviceFlags(xml, flags)
        except lv.libvirtError as e:  # e.g. already gone after an interrupted `up`
            _t.warning("detach install device failed", target=target, error=str(e))
    if has_install_media(dom):
        raise HitkitError(f"could not detach the install media from {dom.name()}",
                          hint="see `hitkit trace --failed last`")


def data_disk_gib(ctx: HostContext, name: str) -> float | None:
    disk = ctx.paths(name).data_disk
    if not disk.exists():
        return None
    return disks.info(disk)["virtual-size"] / 2**30


def check_resize(ctx: HostContext, spec: MachineSpec) -> None:
    """Refuse a data disk smaller than the existing one: shrinking would destroy data."""
    current = data_disk_gib(ctx, spec.name)
    if current is not None and spec.data_disk_gib < current - 0.01:
        raise HitkitError(
            f"{spec.name}'s data disk is {current:.0f} GiB; refusing to shrink it to {spec.data_disk_gib} GiB",
            hint=f"data disks only grow; pass --disk {spec.name.rsplit('-', 1)[0] if spec.name[-1:].isdigit() else spec.name}={current:.0f} or more")


def _reconcile_size(conn, dom, ctx: HostContext, spec: MachineSpec, info: MachineInfo, log) -> tuple[object, bool]:
    """Bring an existing machine to spec's size. Returns (dom, data disk grown)."""
    if (info.cpus, info.memory_mib) != (spec.cpus, spec.memory_mib) and None not in (info.cpus, info.memory_mib):
        dom = redefine(conn, dom, ctx, spec, info.meta)
        when = "at its next boot" if dom.isActive() else "now"
        log(f"{spec.name}: {info.cpus} -> {spec.cpus} vCPUs, {info.memory_mib} -> {spec.memory_mib} MiB "
            f"(applies {when})")
    current = data_disk_gib(ctx, spec.name)
    if current is None or spec.data_disk_gib <= current + 0.01:
        return dom, False
    check_resize(ctx, spec)
    log(f"{spec.name}: growing the data disk {current:.0f} -> {spec.data_disk_gib} GiB")
    device = "sda" if images.is_installer_role(spec.role) else DATA_DEVICE
    if dom.isActive():
        dom.blockResize(device, spec.data_disk_gib * 2**30, _libvirt().VIR_DOMAIN_BLOCK_RESIZE_BYTES)
    else:
        disks.resize(ctx.paths(spec.name).data_disk, spec.data_disk_gib)
    _t.info("data disk grown", machine=spec.name, from_gib=round(current), to_gib=spec.data_disk_gib)
    return dom, True


def grow_filesystem(ctx: HostContext, name: str) -> None:
    """ext4 grows online; the fstab's x-systemd.growfs also does it at every boot."""
    from hitkit.machine import remote

    remote.run(ctx, name, "resize2fs /dev/disk/by-id/virtio-hitkit-data")


def kit_machines(conn, install_id: str, kit: str) -> list[MachineInfo]:
    """Owned machines belonging to a kit, by metadata (covers scaled-up machines too)."""
    return [i for i in list_machines(conn, install_id)
            if i.ownership is Ownership.OWNED and i.meta and i.meta.kit == kit]


def list_machines(conn, install_id: str) -> list[MachineInfo]:
    """Every domain carrying hitkit metadata (owned or indeterminate). Foreign domains are ignored."""
    infos = [_info(d, install_id) for d in conn.listAllDomains()]
    return sorted((i for i in infos if i.ownership is not Ownership.FOREIGN), key=lambda i: i.name)


def get(conn, name: str, install_id: str) -> tuple[object, MachineInfo] | None:
    dom = _lookup(conn, DOMAIN_PREFIX + name)
    return (dom, _info(dom, install_id)) if dom is not None else None


def _require_owned(info: MachineInfo, force: bool = False) -> None:
    if info.ownership is Ownership.FOREIGN:
        raise Conflict(f"domain for '{info.name}' exists but was not created by hitkit")
    if info.ownership is Ownership.INDETERMINATE and not force:
        raise Conflict(
            f"machine '{info.name}' belongs to another hitkit install or has damaged metadata",
            hint="pass --force to act on it anyway",
        )


def up(
    conn, ctx: HostContext, spec: MachineSpec, log: Callable[[str], None],
    extra_creds: dict[str, bytes] | None = None,
    seed: Seed | None = None, probe: Probe | None = None,
) -> MachineInfo:
    """Create the machine if needed, start it if stopped, and wait for readiness.

    Installer roles need ``seed`` (called only when a fresh install starts) and ``probe``
    (polled for readiness: they report nothing on the console)."""
    with trace.span("machine.up", machine=spec.name, role=spec.role):
        found = get(conn, spec.name, ctx.cfg.install_id)
        grown = False
        booted = True  # by us, now: the console will carry a fresh readiness marker
        paths = ctx.paths(spec.name)
        is_installer = images.is_installer_role(spec.role)
        if is_installer and (seed is None or probe is None):
            raise HitkitError(f"{spec.name}: an installer role needs a seed and a readiness probe from its kit")
        if found is None:
            dom = _create(conn, ctx, spec, log, extra_creds or {}, seed=seed)
        else:
            dom, info = found
            _require_owned(info)
            dom, grown = _reconcile_size(conn, dom, ctx, spec, info, log)
            if outdated(ctx, info) and not spec.stateful_os:
                log(f"{spec.name} runs an older image than its role's current one; run `hitkit upgrade {spec.kit}`")
            changed = False
            if not is_installer:
                changed = creds.sync_kit_credentials(paths.creds_dir, extra_creds or {}, ctx.user)
                if changed:
                    dom = redefine(conn, dom, ctx, spec, info.meta)
            if dom.isActive():
                booted = changed
                if changed:
                    log(f"credentials for {spec.name} changed; restarting it to apply them")
                    restart(dom)
            else:
                booted = not _power_on(ctx, dom, spec.name, log)
                if not booted and changed:  # a resumed guest still runs with the old credentials
                    _wait_ready(ctx, dom, spec.name, fresh_boot=False)
                    log(f"credentials for {spec.name} changed; restarting it to apply them")
                    restart(dom)
                    booted = True

        if is_installer:
            if has_install_media(dom):
                log(f"waiting for {spec.name} to install (watch: hitkit logs {spec.name} -f)")
                readiness.wait_installer(paths.console_log, spec.ready_timeout, lambda: bool(dom.isActive()))
                log(f"install completed; detaching the install media from {spec.name}")
                detach_install_media(dom)
                paths.install_disk.unlink(missing_ok=True)
                paths.seed_img.unlink(missing_ok=True)
            log(f"waiting for {spec.name} to report ready")
            _wait_ready(ctx, dom, spec.name, fresh_boot=booted, timeout=spec.ready_timeout, probe=probe)
            if grown:
                log(f"{spec.name}: the disk was grown; the extra space will be available after the next reboot")
            return _info(dom, ctx.cfg.install_id)

        minutes = spec.ready_timeout // 60
        log(f"waiting for {spec.name} to report ready" + (
            f" (its first boot can take up to {minutes} min; watch: hitkit logs {spec.name} -f)"
            if minutes > 5 and found is None else ""))
        _wait_ready(ctx, dom, spec.name, fresh_boot=booted, timeout=spec.ready_timeout)
        if grown:
            grow_filesystem(ctx, spec.name)
        return _info(dom, ctx.cfg.install_id)


def create(
    conn, ctx: HostContext, spec: MachineSpec, log: Callable[[str], None],
    extra_creds: dict[str, bytes], data_backing: Path | None = None, disposable: bool = False,
):
    """Define and start a new machine without waiting for readiness (e.g. job VMs).
    ``data_backing``: a read-only qcow2 the data disk is overlaid on (warm template).
    ``disposable``: the machine's data is worthless after use (job VMs): never kept."""
    return _create(conn, ctx, spec, log, extra_creds, data_backing, disposable or data_backing is not None)


def _create(
    conn, ctx: HostContext, spec: MachineSpec, log: Callable[[str], None],
    extra_creds: dict[str, bytes], data_backing: Path | None = None, disposable: bool = False,
    seed: Seed | None = None,
):
    with trace.span("machine.create", machine=spec.name, role=spec.role) as result:
        image = images.ensure(ctx.user, spec.role, log)
        paths = ctx.paths(spec.name)
        leftover = leftover_os = None
        if paths.root.exists():
            # A directory without a domain: an interrupted create, or a domain that was
            # undefined behind hitkit's back. Its data disk is the machine's newest data.
            if not disposable and paths.data_disk.exists():
                leftover = ensure_private_dir(ctx.vms_dir / KEPT_DIR, ctx.user) / f".{spec.name}-data-leftover.qcow2"
                paths.data_disk.replace(leftover)
                _stash_state(ctx, paths, _sidecar(leftover))
                log(f"{spec.name}: found its data disk without a libvirt domain; reusing it")
                _t.warning("data disk found without a domain; reusing it", machine=spec.name)
            if spec.stateful_os and paths.os_disk.exists():
                leftover_os = ensure_private_dir(ctx.vms_dir / KEPT_DIR, ctx.user) / f".{spec.name}-os-leftover.qcow2"
                paths.os_disk.replace(leftover_os)
            shutil.rmtree(paths.root)
        ensure_private_dir(paths.root, ctx.user)
        subnet = ctx.cfg.network()
        ip = slot_address(subnet, spec.name)
        mac = mac_address(subnet, ip.packed[3])
        dom = None
        reused = reused_os = False
        is_installer = images.is_installer_role(spec.role)
        try:
            log(f"creating disks for {spec.name}")
            if not is_installer:
                kept_os = leftover_os or (newest_kept_disk(ctx, spec.name, "os") if spec.stateful_os else None)
                if kept_os is not None:
                    # This machine's OS disk holds state; bring its own back rather than a fresh one.
                    log(f"reusing {spec.name}'s kept OS disk (its system state lives there)")
                    kept_os.replace(paths.os_disk)
                    reused_os = True
                    image = _image_of(ctx, paths.os_disk) or image
                else:
                    disks.create_overlay(image.path, paths.os_disk)
            kept = None if disposable else newest_kept_disk(ctx, spec.name)
            if data_backing is not None:
                disks.create_overlay(data_backing, paths.data_disk)
            elif leftover is not None:
                leftover.replace(paths.data_disk)
                _restore_state(_sidecar(leftover), paths)
                reused = True
            elif kept is not None:
                log(f"reusing kept data disk for {spec.name}")
                kept.replace(paths.data_disk)
                if _restore_state(_sidecar(kept), paths):
                    log(f"bringing back {spec.name}'s NVRAM and TPM state")
                reused = True
            else:
                disks.create_data_disk(paths.data_disk, spec.data_disk_gib)

            if is_installer:
                if not reused:
                    from hitkit.machine import seed as seed_disk

                    disks.create_overlay(image.path, paths.install_disk)
                    seed_disk.create_seed_image(paths.seed_img, seed())
                elif not paths.tpm_dir.is_dir():
                    log(f"warning: {spec.name}'s TPM state is missing, so it will ask for its disk "
                        f"recovery key on boot (hitkit creds {spec.kit} shows it)")
                paths.tpm_dir.mkdir(exist_ok=True)

            if reused and (data_disk_gib(ctx, spec.name) or 0) < spec.data_disk_gib - 0.01:
                disks.resize(paths.data_disk, spec.data_disk_gib)  # the fs grows at boot (x-systemd.growfs)

            names = []
            if not is_installer:
                machine_creds = creds.build(spec.name, subnet, mac, ctx.ca, creds.ensure_client_key(ctx.user), extra_creds)
                names = creds.write(paths.creds_dir, machine_creds, ctx.user)
                paths.known_hosts.write_text(machine_creds.known_hosts)

            meta = metadata.MachineMeta(
                install_id=ctx.cfg.install_id, kit=spec.kit, kit_version=spec.kit_version,
                machine=spec.name, role=spec.role, image=image.id, address=str(ip),
                created=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                versions=image.versions,
            )
            log(f"defining and starting {spec.domain_name} at {ip}")
            xml = domain_xml(spec, meta, paths, mac, names, install_media=is_installer and not reused)
            _t.debug("defining domain", machine=spec.name, image=image.id, address=str(ip), credentials=names, xml=xml)
            dom = conn.defineXML(xml)
            result["image"] = image.id
            dom.create()
            return dom
        except BaseException:
            if reused:  # a failed create must not cost the data it was handed
                keep_data_disk(ctx, spec.name, "create failed")
            if reused_os:
                keep_data_disk(ctx, spec.name, "create failed", kind="os")
            _teardown(dom, paths, keep_state=is_installer and reused)
            raise


def redefine(conn, dom, ctx: HostContext, spec: MachineSpec, meta: metadata.MachineMeta):
    """Rewrite the domain definition (same UUID) from spec, metadata and the creds on disk."""
    paths = ctx.paths(spec.name)
    ip = slot_address(ctx.cfg.network(), spec.name)
    mac = mac_address(ctx.cfg.network(), ip.packed[3])
    is_installer = images.is_installer_role(spec.role)
    names = [] if is_installer else sorted(p.name for p in paths.creds_dir.iterdir())
    install_media = has_install_media(dom) if is_installer else False
    xml = domain_xml(spec, meta, paths, mac, names, uuid=dom.UUIDString(), install_media=install_media)
    _t.debug("redefining domain", machine=spec.name, image=meta.image, credentials=names, xml=xml)
    return conn.defineXML(xml)


def restart(dom, timeout: float = 60) -> None:
    """Shut down (hard after ``timeout``) and start again, so QEMU re-reads fw_cfg files."""
    _t.info("restarting domain", domain=dom.name())
    if dom.isActive():
        dom.shutdown()
        deadline = time.monotonic() + timeout
        while dom.isActive() and time.monotonic() < deadline:
            time.sleep(1)
        if dom.isActive():
            dom.destroy()
    dom.create()


def outdated(ctx: HostContext, info: MachineInfo) -> bool:
    """True if the machine boots an image older than its role's current configuration."""
    if info.meta is None:
        return False
    try:
        current = images.image_id(info.meta.role, ctx.user)
    except HitkitError:
        return False
    return info.meta.image != current


def upgrade(
    conn, ctx: HostContext, spec: MachineSpec, log: Callable[[str], None],
    extra_creds: dict[str, bytes] | None = None,
) -> bool:
    """Move a machine onto its role's current image, keeping data disk, credentials and
    host key. Rolls back to the previous OS overlay if the new one does not become ready.
    Returns False if the machine was already current."""
    if images.is_installer_role(spec.role):
        raise HitkitError(f"{spec.name} was installed from its vendor's image and updates itself in place",
                          hint="hitkit only pins the version a fresh install starts from")
    if spec.stateful_os:
        raise HitkitError(
            f"{spec.name} keeps system state on its OS disk, so it is never re-imaged",
            hint=f"update it in place: hitkit ssh {spec.name} -- 'apt update && apt full-upgrade'")
    with trace.span("machine.upgrade", machine=spec.name, role=spec.role) as result:
        found = get(conn, spec.name, ctx.cfg.install_id)
        if found is None:
            raise HitkitError(f"no machine named {spec.name!r}", hint=f"hitkit up {spec.kit}")
        dom, info = found
        _require_owned(info)
        image = images.ensure(ctx.user, spec.role, log)
        if info.meta.image == image.id:
            return False
        paths = ctx.paths(spec.name)
        previous = paths.os_disk.with_suffix(".qcow2.prev")
        nvram_prev = paths.nvram.with_suffix(".fd.prev")
        creds_prev = paths.creds_dir.with_name("creds.prev")
        log(f"upgrading {spec.name}: {info.meta.image} -> {image.id}")
        stop(conn, ctx, spec.name)
        paths.os_disk.replace(previous)
        if paths.nvram.exists():
            paths.nvram.replace(nvram_prev)  # boot entries belong to the old ESP; libvirt recreates it
        shutil.rmtree(creds_prev, ignore_errors=True)
        if paths.creds_dir.exists():
            shutil.copytree(paths.creds_dir, creds_prev)  # a rollback must boot with the old set
        try:
            # The new image may expect credentials the old one did not.
            creds.sync_kit_credentials(paths.creds_dir, extra_creds or {}, ctx.user)
            disks.create_overlay(image.path, paths.os_disk)
            meta = replace(info.meta, image=image.id, versions=image.versions)
            dom = redefine(conn, dom, ctx, spec, meta)
            dom.create()
            readiness.wait(paths.console_log, READY_TIMEOUT, lambda: bool(dom.isActive()))
        except BaseException:
            failed_log = paths.root / "console.failed.log"
            if paths.console_log.exists():
                shutil.copyfile(paths.console_log, failed_log)
            log(f"upgrade of {spec.name} failed (boot log kept at {failed_log}); rolling back to {info.meta.image}")
            _t.error("upgrade failed; rolling back", machine=spec.name, exc_info=True)
            if dom.isActive():
                dom.destroy()
            paths.os_disk.unlink(missing_ok=True)
            previous.replace(paths.os_disk)
            paths.nvram.unlink(missing_ok=True)
            if nvram_prev.exists():
                nvram_prev.replace(paths.nvram)
            if creds_prev.exists():
                shutil.rmtree(paths.creds_dir, ignore_errors=True)
                creds_prev.replace(paths.creds_dir)
            dom = redefine(conn, dom, ctx, spec, info.meta)
            dom.create()
            try:
                readiness.wait(paths.console_log, READY_TIMEOUT, lambda: bool(dom.isActive()))
                log(f"{spec.name} is back on {info.meta.image}")
            except readiness.NotReady as e:
                log(f"warning: {spec.name} did not become ready after rollback either: {e}")
            raise
        previous.unlink(missing_ok=True)
        nvram_prev.unlink(missing_ok=True)
        shutil.rmtree(creds_prev, ignore_errors=True)
        return True


def _teardown(dom, paths: MachinePaths, keep_state: bool = False) -> None:
    """Undefine the domain and remove the machine directory. ``keep_state``: libvirt must not
    delete NVRAM or TPM state (an installer machine's disk key is sealed to them; the caller
    has moved them aside). Missing flags fail loudly rather than fall back to deleting."""
    lv = _libvirt()
    if keep_state:
        flags = lv.VIR_DOMAIN_UNDEFINE_KEEP_NVRAM | lv.VIR_DOMAIN_UNDEFINE_KEEP_TPM
    else:
        flags = lv.VIR_DOMAIN_UNDEFINE_NVRAM
    if dom is not None:
        try:
            if dom.isActive():
                dom.destroy()
            dom.undefineFlags(flags | lv.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE)
        except lv.libvirtError:
            pass
    shutil.rmtree(paths.root, ignore_errors=True)


def start(conn, ctx: HostContext, name: str, log: Callable[[str], None], probe: Probe | None = None) -> None:
    """Start an existing machine and wait until it reports ready (``probe``: as for `up`)."""
    with trace.span("machine.start", machine=name):
        found = get(conn, name, ctx.cfg.install_id)
        if found is None:
            raise HitkitError(f"no machine named {name!r}", hint="hitkit status")
        dom, info = found
        _require_owned(info)
        if probe is None and images.is_installer_role(info.meta.role):
            raise HitkitError(f"{name}: an installer role needs a readiness probe from its kit")
        booted = False if dom.isActive() else not _power_on(ctx, dom, name, log)
        log(f"waiting for {name} to report ready")
        _wait_ready(ctx, dom, name, fresh_boot=booted, probe=probe)


# --- saved state ------------------------------------------------------------------
# A user session's libvirt saves running VMs to disk when the session ends (libvirt's
# auto_shutdown_try_save, "persistent" by default), and starting such a domain *resumes*
# it: no boot, so no readiness marker on the console. Resuming keeps every unflushed
# write; discarding the save would be a power cut. Anything that touches disks offline
# (upgrade, offline backup and restore, destroy) first resumes and shuts down cleanly.

RESUME_TIMEOUT = 120


def has_saved_state(dom) -> bool:
    try:
        return bool(dom.hasManagedSaveImage(0))
    except Exception:
        return False


def _power_on(ctx: HostContext, dom, name: str, log: Callable[[str], None]) -> bool:
    """Start the domain; True if it resumed from saved state rather than booting."""
    resumed = has_saved_state(dom)
    log(f"resuming {name} from the state libvirt saved when your session ended" if resumed
        else f"starting {name}")
    _t.info("powering on", machine=name, resumed=resumed)
    dom.create()
    if resumed:
        try:  # the guest clock stopped while saved; the guest agent sets it right
            dom.setTime(flags=_libvirt().VIR_DOMAIN_TIME_SYNC)
        except Exception as e:
            _t.info("could not sync the clock after resume (timesyncd will)", machine=name, error=str(e))
    return resumed


def _wait_probe(ctx: HostContext, dom, name: str, probe: Probe, timeout: int) -> None:
    """Poll the kit's probe (installer roles report nothing on the console) until it passes."""
    deadline = time.monotonic() + timeout
    reason = "no answer yet"
    while True:
        if not dom.isActive():
            raise readiness.NotReady(f"{name} stopped before reporting ready", hint=f"see {ctx.paths(name).console_log}")
        try:
            reason = probe()
        except HitkitError as e:
            reason = str(e)
        if reason is None:
            _t.info("machine ready (probe)", machine=name)
            return
        if time.monotonic() > deadline:
            _t.error("machine not ready (probe)", machine=name, reason=reason)
            raise readiness.NotReady(f"{name} did not become ready in {timeout}s: {reason}",
                                     hint=f"see {ctx.paths(name).console_log}")
        time.sleep(2)


def _wait_ready(ctx: HostContext, dom, name: str, fresh_boot: bool, timeout: int = READY_TIMEOUT,
                probe: Probe | None = None) -> None:
    """With a ``probe``, poll it. Otherwise: after a boot hitkit started, the console carries
    the verdict; if not (already running, or resumed: the console log restarts empty) ask the
    guest over SSH."""
    if probe is not None:
        _wait_probe(ctx, dom, name, probe, timeout)
        return
    if fresh_boot:
        readiness.wait(ctx.paths(name).console_log, timeout, lambda: bool(dom.isActive()))
        return
    from hitkit.machine import remote

    deadline = time.monotonic() + RESUME_TIMEOUT
    while True:
        try:
            # A gate that failed earlier (e.g. a first-boot install that has since been
            # fixed) is re-run rather than reported stale; restart waits for the verdict.
            remote.run(ctx, name, "systemctl is-active --quiet hitkit-ready.service "
                                  "|| systemctl restart hitkit-ready.service")
            _t.info("resumed machine ready", machine=name)
            return
        except HitkitError as e:
            if time.monotonic() > deadline or not dom.isActive():
                raise readiness.NotReady(f"{name} is not ready: {e}",
                                         hint=f"hitkit ssh {name} -- systemctl status hitkit-ready") from None
        time.sleep(2)


def stop(conn, ctx: HostContext, name: str, timeout: float = 60) -> bool:
    """Graceful ACPI shutdown, then hard power-off after ``timeout``. Returns False if absent."""
    with trace.span("machine.stop", machine=name):
        found = get(conn, name, ctx.cfg.install_id)
        if found is None:
            return False
        dom, info = found
        _require_owned(info)
        if not dom.isActive():
            if not has_saved_state(dom):
                return True
            _t.info("resuming saved state to shut down cleanly", machine=name)
            dom.create()  # its disks must not change under a saved memory image
        dom.shutdown()
        deadline = time.monotonic() + timeout
        while dom.isActive() and time.monotonic() < deadline:
            time.sleep(1)
        if dom.isActive():
            dom.destroy()
        return True


KEPT_DIR = ".kept"


def _kept_data_disk(ctx: HostContext, name: str) -> Path:
    return ctx.vms_dir / KEPT_DIR / f"{name}-data.qcow2"


_KEPT_NAME = re.compile(r"^\.?(?P<machine>.+?)-(?P<kind>data|os)(?:-\d{8}T\d{6}Z|-leftover)?\.qcow2$")


def newest_kept_disk(ctx: HostContext, name: str, kind: str = "data") -> Path | None:
    """The kept disk the next create of ``name`` reuses: the one with the newest data
    (a rename keeps a file's mtime, so mtime is when the machine last wrote to it)."""
    candidates = [k.path for k in kept_disks(ctx) if k.machine == name and k.kind == kind]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def keep_data_disk(ctx: HostContext, name: str, reason: str = "kept on request", kind: str = "data") -> Path | None:
    """Move a machine's data disk (or, kind="os", its OS disk) to vms/.kept/, where the next
    create of that machine reuses the newest one.

    Never overwrites: if a kept disk of that kind already exists, this one gets a
    timestamped name. Returns where the disk went, or None if there was no such disk."""
    paths = ctx.paths(name)
    disk = paths.data_disk if kind == "data" else paths.os_disk
    if not disk.exists():
        return None
    dest = ctx.vms_dir / KEPT_DIR / f"{name}-{kind}.qcow2"
    ensure_private_dir(dest.parent, ctx.user)
    if dest.exists():
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = dest.with_name(f"{name}-{kind}-{stamp}.qcow2")
    disk.replace(dest)
    if kind == "data":
        _stash_state(ctx, paths, _sidecar(dest))
    _t.warning("disk kept", machine=name, kind=kind, reason=reason, path=dest)
    return dest


# An installer machine's disk is encrypted with a key sealed to its TPM, measured against
# the Secure Boot state in its NVRAM (docs/incus/incus-os-in-libvirt.md §5): without both,
# it boots only with its recovery key. So they travel with the disk, in a sidecar
# directory next to the kept disk. Only installer machines have a TPM directory.

def _sidecar(disk: Path) -> Path:
    return disk.with_suffix(".state")


def _stash_state(ctx: HostContext, paths: MachinePaths, sidecar: Path) -> None:
    if not paths.tpm_dir.is_dir():
        return
    ensure_private_dir(sidecar, ctx.user)
    if paths.nvram.exists():
        paths.nvram.replace(sidecar / "nvram.fd")
    shutil.rmtree(sidecar / "tpm", ignore_errors=True)
    paths.tpm_dir.replace(sidecar / "tpm")


def _restore_state(sidecar: Path, paths: MachinePaths) -> bool:
    """Bring a kept disk's NVRAM and TPM state back; False if it has none."""
    if not (sidecar / "tpm").is_dir():
        return False
    if (sidecar / "nvram.fd").exists():
        (sidecar / "nvram.fd").replace(paths.nvram)
    shutil.rmtree(paths.tpm_dir, ignore_errors=True)
    (sidecar / "tpm").replace(paths.tpm_dir)
    shutil.rmtree(sidecar)
    return True


def delete_kept_disk(path: Path) -> None:
    """Delete a kept disk and its NVRAM and TPM state, if any (the state holds key material)."""
    path.unlink()
    shutil.rmtree(_sidecar(path), ignore_errors=True)


def keep_disks(ctx: HostContext, name: str, reason: str, stateful_os: bool) -> list[Path]:
    kept = [keep_data_disk(ctx, name, reason)]
    if stateful_os:
        kept.append(keep_data_disk(ctx, name, reason, kind="os"))
    return [k for k in kept if k is not None]


@dataclass(frozen=True)
class KeptDisk:
    machine: str
    path: Path
    size_bytes: int
    modified: str
    reused_by_up: bool  # the newest of its machine and kind: what the next create picks up
    kind: str = "data"


def kept_disks(ctx: HostContext) -> list[KeptDisk]:
    kept_dir = ctx.vms_dir / KEPT_DIR
    if not kept_dir.is_dir():
        return []
    found = []
    for path in kept_dir.glob("*.qcow2"):
        if m := _KEPT_NAME.match(path.name):
            found.append((m["machine"], m["kind"], path, path.stat()))
    newest: dict[tuple[str, str], float] = {}
    for machine, kind, _, st in found:
        newest[(machine, kind)] = max(newest.get((machine, kind), 0), st.st_mtime)
    result = [
        KeptDisk(machine, path, st.st_size,
                 dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat(timespec="seconds"),
                 st.st_mtime == newest[(machine, kind)], kind)
        for machine, kind, path, st in found
    ]
    return sorted(result, key=lambda k: (k.machine, k.kind, not k.reused_by_up, k.path.name))


def destroy(conn, ctx: HostContext, name: str, force: bool = False, delete_data: bool = False,
            stateful_os: bool = False) -> bool:
    """Remove the domain and the machine directory. Returns False if there was nothing to remove.

    The data disk is kept (vms/.kept/) unless ``delete_data``; that holds even when the
    domain is already gone and only the directory is left."""
    with trace.span("machine.destroy", machine=name, force=force, delete_data=delete_data):
        found = get(conn, name, ctx.cfg.install_id)
        paths = ctx.paths(name)
        if found is None and not paths.root.exists():
            return False
        if found is not None:
            _require_owned(found[1], force)
        keep_state = not delete_data and paths.tpm_dir.is_dir()  # moved aside with the kept disk
        if not delete_data:
            if found is not None:
                stop(conn, ctx, name)  # a clean shutdown flushes everything to the kept disk
            keep_disks(ctx, name, "destroyed without --delete-data", stateful_os)
        _teardown(found[0] if found else None, paths, keep_state=keep_state)
        return True


def orphan_dirs(conn, ctx: HostContext) -> list[Path]:
    """Machine directories that no domain claims at all.

    Any domain counts, owned or not: a directory whose domain belongs to another
    install ID (e.g. after config.toml was recreated) is not an orphan."""
    if not ctx.vms_dir.exists():
        return []
    claimed = {d.name().removeprefix(DOMAIN_PREFIX) for d in conn.listAllDomains()}
    claimed |= {i.name for i in list_machines(conn, ctx.cfg.install_id)}
    return sorted(
        d for d in ctx.vms_dir.iterdir()
        if d.is_dir() and d.name not in claimed and not d.name.startswith(".")
    )


def remove_orphan(ctx: HostContext, path: Path, stateful_os: bool = False) -> list[Path]:
    """Delete an orphaned machine directory, keeping its data disk (and, for machines whose
    OS disk holds state, that too). Returns the kept disks.

    A data disk that is an overlay (a job VM's, on the warm template) holds nothing unique."""
    kept = []
    if not _is_overlay(path / "data.qcow2"):
        kept = keep_disks(ctx, path.name, "orphaned directory removed by gc", stateful_os)
    shutil.rmtree(path)
    return kept


def _image_of(ctx: HostContext, os_disk: Path):
    """The cached image a (reused) OS disk is an overlay of, so metadata stays truthful."""
    try:
        chain = disks.backing_chain(os_disk)
    except HitkitError:
        return None
    if chain and not chain[0].exists():
        raise HitkitError(f"{os_disk} needs its base image {chain[0]}, which is gone",
                          hint="restore the image file, or move the OS disk out of vms/.kept to start fresh")
    for img in images.list_images(ctx.user):
        if chain and img.path.resolve() == chain[0].resolve():
            return img
    return None


def _is_overlay(disk: Path) -> bool:
    if not disk.exists():
        return False
    try:
        return bool(disks.backing_chain(disk))
    except HitkitError:
        return False  # unreadable: treat as unique data and keep it


def unreferenced_images(ctx: HostContext) -> list[images.Image]:
    """Cached images that are neither current for their role nor backing any machine disk."""
    referenced: set[Path] = set()
    if ctx.vms_dir.exists():
        for disk in [*ctx.vms_dir.glob("*/os.qcow2"), *ctx.vms_dir.glob(f"{KEPT_DIR}/*os*.qcow2")]:
            referenced.update(p.resolve() for p in disks.backing_chain(disk))
    unused = []
    for image in images.list_images(ctx.user):
        current = images.find(ctx.user, image.role) if image.role in _known_roles() else None
        if image.path.resolve() not in referenced and (current is None or current.id != image.id):
            unused.append(image)
    return unused


def _known_roles() -> set[str]:
    return {d.name for d in images.IMAGES_DIR.iterdir() if (d / "mkosi.conf").is_file() or (d / "installer.toml").is_file()}


def require_session_ready(conn) -> None:
    """Sanity check before creating machines: the hitkit bridge must exist."""
    from hitkit.host.network import BRIDGE_NAME

    if not Path(f"/sys/class/net/{BRIDGE_NAME}").exists():
        raise HitkitError(f"bridge {BRIDGE_NAME} does not exist", hint="sudo hitkit setup")
