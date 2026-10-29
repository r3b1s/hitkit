"""libvirt domain XML for a machine (design §8.3, §8.5)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from hitkit.addressing import parse_subnet
from hitkit.machine import metadata
from hitkit.machine.domain import CRED_PREFIX, domain_xml
from hitkit.machine.metadata import MachineMeta
from hitkit.machine.spec import MachinePaths, MachineSpec, mac_address

NS = "urn:hitkit:metadata:1"
SUBNET = parse_subnet("10.77.0.0/24")
CREDS = ["hitkit.network", "system.hostname"]


@pytest.fixture
def spec() -> MachineSpec:
    return MachineSpec(
        name="devbox", role="base", cpus=2, memory_mib=2048, data_disk_gib=20,
        kit="devbox", kit_version=1,
    )


@pytest.fixture
def meta() -> MachineMeta:
    return MachineMeta(
        install_id="install-abc", kit="devbox", kit_version=1, machine="devbox",
        role="base", image="base-0123456789ab", address="10.77.0.40",
        created="2026-10-12T00:00:00Z", versions={"systemd": "257"},
    )


@pytest.fixture
def xml_and_paths(spec, meta, tmp_path):
    paths = MachinePaths(tmp_path / "devbox")
    mac = mac_address(SUBNET, 40)
    text = domain_xml(spec, meta, paths, mac, CREDS)
    return ET.fromstring(text), paths, mac


def test_domain_identity_memory_and_cpus(xml_and_paths):
    root, _, _ = xml_and_paths
    assert root.findtext("name") == "hitkit-devbox"
    memory = root.find("memory")
    assert memory.get("unit") == "MiB"
    assert memory.text == "2048"
    assert root.findtext("vcpu") == "2"


def test_firmware_is_efi_with_secure_boot_disabled_and_nvram_path(xml_and_paths):
    root, paths, _ = xml_and_paths
    os_el = root.find("os")
    assert os_el.get("firmware") == "efi"
    features = {f.get("name"): f.get("enabled") for f in os_el.findall("firmware/feature")}
    assert features["secure-boot"] == "no"
    assert os_el.findtext("nvram") == str(paths.nvram)


def test_two_virtio_disks_os_and_data(xml_and_paths):
    root, paths, _ = xml_and_paths
    disks = {d.find("target").get("dev"): d for d in root.findall("devices/disk")}
    assert set(disks) == {"vda", "vdb"}
    assert disks["vda"].find("source").get("file") == str(paths.os_disk)
    assert disks["vdb"].find("source").get("file") == str(paths.data_disk)
    assert disks["vdb"].findtext("serial") == "hitkit-data"
    assert all(d.find("target").get("bus") == "virtio" for d in disks.values())


def test_bridge_interface_uses_hitkit_bridge_and_mac(xml_and_paths):
    root, _, mac = xml_and_paths
    iface = root.find("devices/interface")
    assert iface.get("type") == "bridge"
    assert iface.find("source").get("bridge") == "hitkit0"
    assert iface.find("mac").get("address") == mac


def test_serial_console_is_logged_to_console_log(xml_and_paths):
    root, paths, _ = xml_and_paths
    assert root.find("devices/serial/log").get("file") == str(paths.console_log)
    assert paths.console_log.name == "console.log"


def test_credentials_are_file_references_with_no_contents(xml_and_paths):
    root, paths, _ = xml_and_paths
    entries = root.findall("sysinfo[@type='fwcfg']/entry")
    assert {e.get("name") for e in entries} == {CRED_PREFIX + n for n in CREDS}
    for entry in entries:
        name = entry.get("name").removeprefix(CRED_PREFIX)
        assert entry.get("file") == str(paths.creds_dir / name)
        # Security: the secret never travels in the XML, only its path.
        assert entry.text is None or entry.text.strip() == ""
        assert len(entry) == 0


def test_no_sysinfo_without_credentials(spec, meta, tmp_path):
    text = domain_xml(spec, meta, MachinePaths(tmp_path / "devbox"), mac_address(SUBNET, 40), [])
    assert ET.fromstring(text).find("sysinfo") is None


def test_hitkit_metadata_is_embedded(xml_and_paths, meta):
    root, _, _ = xml_and_paths
    element = root.find("metadata/{%s}machine" % NS)
    assert element is not None
    assert metadata.from_xml(ET.tostring(element, encoding="unicode")) == meta


def test_mac_address_uses_third_octet_as_documented():
    assert mac_address(parse_subnet("10.77.0.0/24"), 10) == "52:54:00:68:00:0a"
    assert mac_address(parse_subnet("10.77.5.0/24"), 10) == "52:54:00:68:05:0a"


# --- machine spec (design §8.2) ---------------------------------------------


def test_spec_rejects_unknown_slot_name():
    with pytest.raises(KeyError):
        MachineSpec(
            name="not-a-slot", role="base", cpus=1, memory_mib=512, data_disk_gib=1,
            kit="k", kit_version=1,
        )


def test_spec_domain_name_is_prefixed(spec):
    assert spec.domain_name == "hitkit-devbox"


def test_machine_paths_file_names_under_root(tmp_path):
    paths = MachinePaths(tmp_path)
    assert paths.os_disk == tmp_path / "os.qcow2"
    assert paths.data_disk == tmp_path / "data.qcow2"
    assert paths.nvram == tmp_path / "nvram.fd"
    assert paths.creds_dir == tmp_path / "creds"
    assert paths.known_hosts == tmp_path / "known_hosts"
    assert paths.console_log == tmp_path / "console.log"
    assert paths.install_disk == tmp_path / "install.qcow2"
    assert paths.seed_img == tmp_path / "seed.img"
    assert paths.tpm_dir == tmp_path / "tpm"


def test_machine_paths_for_machine_nests_under_vms_dir(tmp_path):
    assert MachinePaths.for_machine(tmp_path, "devbox").root == tmp_path / "devbox"


# --- installer domain XML (plan §4.2, §9) -----------------------------------


@pytest.fixture
def installer_spec() -> MachineSpec:
    return MachineSpec(
        name="incus-os", role="incus-os", cpus=4, memory_mib=8192, data_disk_gib=150,
        kit="incus-os", kit_version=1, stateful_os=True, ready_timeout=600,
    )


@pytest.fixture
def installer_meta() -> MachineMeta:
    return MachineMeta(
        install_id="install-abc", kit="incus-os", kit_version=1, machine="incus-os",
        role="incus-os", image="incus-os-0123456789ab", address="10.77.0.2",
        created="2026-10-29T00:00:00Z", versions={"incus-os": "202610272235"},
    )


def test_installer_domain_xml_features_and_devices(installer_spec, installer_meta, tmp_path):
    paths = MachinePaths(tmp_path / "incus-os")
    mac = mac_address(SUBNET, 2)
    xml_text = domain_xml(installer_spec, installer_meta, paths, mac, ["cred1"], install_media=True)
    root = ET.fromstring(xml_text)

    # Secure Boot enabled, enrolled keys disabled, SMM on
    os_el = root.find("os")
    assert os_el.get("firmware") == "efi"
    assert os_el.find("boot") is None  # no <boot dev="hd"/>
    features = {f.get("name"): f.get("enabled") for f in os_el.findall("firmware/feature")}
    assert features["secure-boot"] == "yes"
    assert features["enrolled-keys"] == "no"
    assert root.find("features/smm").get("state") == "on"

    # No fw_cfg credentials sysinfo, no guest agent channel, no graphics
    assert root.find("sysinfo") is None
    assert root.find("devices/channel") is None
    assert root.find("devices/graphics") is None
    assert root.find("devices/video") is None

    # TPM with emulator 2.0 and dir source
    tpm = root.find("devices/tpm")
    assert tpm.get("model") == "tpm-crb"
    backend = tpm.find("backend")
    assert backend.get("type") == "emulator"
    assert backend.get("version") == "2.0"
    assert backend.get("persistent_state") == "yes"
    assert backend.find("source").get("type") == "dir"
    assert backend.find("source").get("path") == str(paths.tpm_dir)

    # Controllers
    controllers = {c.get("type"): c.get("model") for c in root.findall("devices/controller")}
    assert controllers.get("scsi") == "virtio-scsi"
    assert controllers.get("usb") == "qemu-xhci"

    # Disks with install media: sda (scsi), sdb (usb install), sdc (usb seed)
    disks = {d.find("target").get("dev"): d for d in root.findall("devices/disk")}
    assert set(disks) == {"sda", "sdb", "sdc"}

    sda = disks["sda"]
    assert sda.find("source").get("file") == str(paths.data_disk)
    assert sda.find("target").get("bus") == "scsi"
    assert sda.findtext("serial") == "hitkit-data"
    assert sda.find("boot").get("order") == "1"

    sdb = disks["sdb"]
    assert sdb.find("source").get("file") == str(paths.install_disk)
    assert sdb.find("target").get("bus") == "usb"
    assert sdb.find("target").get("removable") == "on"
    assert sdb.find("boot").get("order") == "2"

    sdc = disks["sdc"]
    assert sdc.find("source").get("file") == str(paths.seed_img)
    assert sdc.find("driver").get("type") == "raw"
    assert sdc.find("target").get("bus") == "usb"
    assert sdc.find("target").get("removable") == "on"


def test_installer_domain_xml_without_install_media(installer_spec, installer_meta, tmp_path):
    paths = MachinePaths(tmp_path / "incus-os")
    mac = mac_address(SUBNET, 2)
    xml_text = domain_xml(installer_spec, installer_meta, paths, mac, [], install_media=False)
    root = ET.fromstring(xml_text)

    disks = {d.find("target").get("dev"): d for d in root.findall("devices/disk")}
    assert set(disks) == {"sda"}

