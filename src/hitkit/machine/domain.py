"""libvirt domain XML for a hitkit machine (design §8.3, §8.5).

Session-mode domains: q35, UEFI (OVMF via firmware autoselection, secure boot
off), virtio everything, a bridge interface on hitkit0 (attached through
qemu-bridge-helper), the serial console logged to a file, and credentials
passed by *file reference* through fw_cfg so secrets never appear in the XML.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from hitkit.host.network import BRIDGE_NAME
from hitkit.machine import metadata
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
) -> str:
    dom = ET.Element("domain", type="kvm")
    ET.SubElement(dom, "name").text = spec.domain_name
    if uuid:  # redefining an existing domain must keep its UUID
        ET.SubElement(dom, "uuid").text = uuid
    ET.SubElement(dom, "title").text = f"hitkit {spec.kit}/{spec.name}"
    md = ET.SubElement(dom, "metadata")
    md.append(metadata.to_element(meta))
    ET.SubElement(dom, "memory", unit="MiB").text = str(spec.memory_mib)
    ET.SubElement(dom, "vcpu").text = str(spec.cpus)

    if credential_names:
        sysinfo = ET.SubElement(dom, "sysinfo", type="fwcfg")
        for name in credential_names:
            ET.SubElement(sysinfo, "entry", name=CRED_PREFIX + name, file=str(paths.creds_dir / name))

    os_el = ET.SubElement(dom, "os", firmware="efi")
    ET.SubElement(os_el, "type", arch="x86_64", machine="q35").text = "hvm"
    fw = ET.SubElement(os_el, "firmware")
    ET.SubElement(fw, "feature", enabled="no", name="secure-boot")
    ET.SubElement(fw, "feature", enabled="no", name="enrolled-keys")
    ET.SubElement(os_el, "nvram").text = str(paths.nvram)
    ET.SubElement(os_el, "boot", dev="hd")

    features = ET.SubElement(dom, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    ET.SubElement(dom, "cpu", mode="host-passthrough")
    ET.SubElement(dom, "clock", offset="utc")
    ET.SubElement(dom, "on_poweroff").text = "destroy"
    ET.SubElement(dom, "on_reboot").text = "restart"
    ET.SubElement(dom, "on_crash").text = "destroy"

    devices = ET.SubElement(dom, "devices")
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

    channel = ET.SubElement(devices, "channel", type="unix")
    ET.SubElement(channel, "target", type="virtio", name="org.qemu.guest_agent.0")

    rng = ET.SubElement(devices, "rng", model="virtio")
    ET.SubElement(rng, "backend", model="random").text = "/dev/urandom"
    ET.SubElement(devices, "memballoon", model="virtio")

    ET.indent(dom)
    return ET.tostring(dom, encoding="unicode")


def _disk(devices: ET.Element, path, target: str, serial: str) -> None:
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2", discard="unmap")
    ET.SubElement(disk, "source", file=str(path))
    ET.SubElement(disk, "target", dev=target, bus="virtio")
    ET.SubElement(disk, "serial").text = serial
