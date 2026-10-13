"""hitkit metadata in libvirt domain XML, and ownership classification (design §8.4).

libvirt is the source of truth: a domain is hitkit's only if its metadata says
so *and* names this install. Names are never trusted.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum

NS = "urn:hitkit:metadata:1"
PREFIX = "hitkit"
ET.register_namespace(PREFIX, NS)

_FIELDS = ("install-id", "kit", "kit-version", "machine", "role", "image", "address", "created")


@dataclass(frozen=True)
class MachineMeta:
    install_id: str
    kit: str
    kit_version: int
    machine: str
    role: str
    image: str
    address: str
    created: str
    versions: dict[str, str] = field(default_factory=dict)


class Ownership(Enum):
    OWNED = "owned"
    FOREIGN = "foreign"
    INDETERMINATE = "indeterminate"


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def to_element(meta: MachineMeta) -> ET.Element:
    root = ET.Element(_q("machine"))
    values = {
        "install-id": meta.install_id, "kit": meta.kit, "kit-version": str(meta.kit_version),
        "machine": meta.machine, "role": meta.role, "image": meta.image,
        "address": meta.address, "created": meta.created,
    }
    for name in _FIELDS:
        ET.SubElement(root, _q(name)).text = values[name]
    versions = ET.SubElement(root, _q("versions"))
    for comp, ver in sorted(meta.versions.items()):
        ET.SubElement(versions, _q("component"), name=comp).text = ver
    return root


def from_xml(xml: str) -> MachineMeta:
    """Parse the metadata element as returned by libvirt. Raises ValueError if malformed."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ValueError(f"unparseable hitkit metadata: {e}") from None
    # The domain XML carries our namespace, but virDomainGetMetadata returns the
    # element with it stripped. Accept both by comparing local names only.
    for el in root.iter():
        el.tag = el.tag.rpartition("}")[2]
    if root.tag != "machine":
        raise ValueError(f"unexpected metadata root {root.tag}")
    values = {}
    for name in _FIELDS:
        el = root.find(name)
        if el is None or not el.text:
            raise ValueError(f"hitkit metadata missing {name}")
        values[name] = el.text
    versions = {c.get("name"): c.text or "" for c in root.findall("versions/component")}
    try:
        kit_version = int(values["kit-version"])
    except ValueError:
        raise ValueError("hitkit metadata kit-version is not an integer") from None
    return MachineMeta(
        install_id=values["install-id"], kit=values["kit"], kit_version=kit_version,
        machine=values["machine"], role=values["role"], image=values["image"],
        address=values["address"], created=values["created"], versions=versions,
    )


def classify(meta_xml: str | None, install_id: str) -> tuple[Ownership, MachineMeta | None]:
    """owned: our metadata and our install; foreign: no hitkit metadata; else indeterminate."""
    if meta_xml is None:
        return Ownership.FOREIGN, None
    try:
        meta = from_xml(meta_xml)
    except ValueError:
        return Ownership.INDETERMINATE, None
    if meta.install_id != install_id:
        return Ownership.INDETERMINATE, meta
    return Ownership.OWNED, meta
