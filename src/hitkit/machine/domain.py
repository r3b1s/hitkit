"""libvirt domain XML for a hitkit machine (design §8.3, §8.5, plan §4.2).

Session-mode domains: q35, UEFI (OVMF via firmware autoselection),
virtio network on hitkit0 (attached through qemu-bridge-helper), and the serial
console logged to a file.

For standard roles: secure boot off, virtio disks (vda overlay on cached image,
vdb data disk), guest agent channel, credentials passed through fw_cfg.

For installer roles: secure boot on (no enrolled keys), SMM, TPM 2.0 emulator,
virtio-scsi disk with boot order 1, USB install media when installing, no fw_cfg,
no guest agent, and no graphics device.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from hitkit.host.network import BRIDGE_NAME
from hitkit.machine import images, metadata
from hitkit.machine.spec import MachinePaths, MachineSpec

CRED_PREFIX = "opt/io.systemd.credentials/"
DATA_DISK_SERIAL = "hitkit-data"


def domain_xml(
    spec: MachineSpec,
    meta: metadata.MachineMeta,
    paths: MachinePaths,
    mac: str,
    credential_names: list[str],
    uuid: str | None = None,
    install_media: bool = False,
) -> str:
    is_installer = images.is_installer_role(spec.role)

    dom = ET.Element("domain", type="kvm")
    ET.SubElement(dom, "name").text = spec.domain_name
    if uuid:  # redefining an existing domain must keep its UUID
        ET.SubElement(dom, "uuid").text = uuid
    ET.SubElement(dom, "title").text = f"hitkit {spec.kit}/{spec.name}"
    md = ET.SubElement(dom, "metadata")
    md.append(metadata.to_element(meta))
    ET.SubElement(dom, "memory", unit="MiB").text = str(spec.memory_mib)
    ET.SubElement(dom, "vcpu").text = str(spec.cpus)

    if credential_names and not is_installer:
        sysinfo = ET.SubElement(dom, "sysinfo", type="fwcfg")
        for name in credential_names:
            ET.SubElement(sysinfo, "entry", name=CRED_PREFIX + name, file=str(paths.creds_dir / name))

    os_el = ET.SubElement(dom, "os", firmware="efi")
    ET.SubElement(os_el, "type", arch="x86_64", machine="q35").text = "hvm"
    fw = ET.SubElement(os_el, "firmware")
    ET.SubElement(fw, "feature", enabled="yes" if is_installer else "no", name="secure-boot")
    ET.SubElement(fw, "feature", enabled="no", name="enrolled-keys")
    ET.SubElement(os_el, "nvram").text = str(paths.nvram)
    if not is_installer:
        ET.SubElement(os_el, "boot", dev="hd")

    features = ET.SubElement(dom, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    if is_installer:
        ET.SubElement(features, "smm", state="on")
    ET.SubElement(dom, "cpu", mode="host-passthrough")
    ET.SubElement(dom, "clock", offset="utc")
    ET.SubElement(dom, "on_poweroff").text = "destroy"
    ET.SubElement(dom, "on_reboot").text = "restart"
    ET.SubElement(dom, "on_crash").text = "destroy"

    devices = ET.SubElement(dom, "devices")
    if is_installer:
        ET.SubElement(devices, "controller", type="scsi", model="virtio-scsi")
        ET.SubElement(devices, "controller", type="usb", model="qemu-xhci")
        _installer_disk(
            devices,
            paths.data_disk,
            target="sda",
            serial=DATA_DISK_SERIAL,
            bus="scsi",
            boot_order="1",
            discard="unmap",
            driver_type="qcow2",
        )
        if install_media:
            _installer_disk(
                devices,
                paths.install_disk,
                target="sdb",
                bus="usb",
                removable="on",
                boot_order="2",
                driver_type="qcow2",
            )
            _installer_disk(
                devices,
                paths.seed_img,
                target="sdc",
                bus="usb",
                removable="on",
                driver_type="raw",
            )
    else:
        _disk(devices, paths.os_disk, "vda", "hitkit-os")
        _disk(devices, paths.data_disk, "vdb", DATA_DISK_SERIAL)

    iface = ET.SubElement(devices, "interface", type="bridge")
    ET.SubElement(iface, "source", bridge=BRIDGE_NAME)
    ET.SubElement(iface, "mac", address=mac)
    ET.SubElement(iface, "model", type="virtio")

    serial = ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(serial, "log", file=str(paths.console_log), append="off")
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")

    if not is_installer:
        channel = ET.SubElement(devices, "channel", type="unix")
        ET.SubElement(channel, "target", type="virtio", name="org.qemu.guest_agent.0")

    if is_installer:
        tpm = ET.SubElement(devices, "tpm", model="tpm-crb")
        backend = ET.SubElement(tpm, "backend", type="emulator", version="2.0", persistent_state="yes")
        ET.SubElement(backend, "source", type="dir", path=str(paths.tpm_dir))

    rng = ET.SubElement(devices, "rng", model="virtio")
    ET.SubElement(rng, "backend", model="random").text = "/dev/urandom"
    ET.SubElement(devices, "memballoon", model="virtio")

    ET.indent(dom)
    return ET.tostring(dom, encoding="unicode")


def _disk(devices: ET.Element, path: Path, target: str, serial: str) -> None:
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2", discard="unmap")
    ET.SubElement(disk, "source", file=str(path))
    ET.SubElement(disk, "target", dev=target, bus="virtio")
    ET.SubElement(disk, "serial").text = serial


def _installer_disk(
    devices: ET.Element,
    path: Path,
    target: str,
    serial: str | None = None,
    bus: str = "scsi",
    boot_order: str | None = None,
    removable: str | None = None,
    driver_type: str = "qcow2",
    discard: str | None = None,
) -> None:
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    driver_attrs = {"name": "qemu", "type": driver_type}
    if discard:
        driver_attrs["discard"] = discard
    ET.SubElement(disk, "driver", **driver_attrs)
    ET.SubElement(disk, "source", file=str(path))
    target_attrs = {"dev": target, "bus": bus}
    if removable:
        target_attrs["removable"] = removable
    ET.SubElement(disk, "target", **target_attrs)
    if serial:
        ET.SubElement(disk, "serial").text = serial
    if boot_order:
        ET.SubElement(disk, "boot", order=boot_order)
