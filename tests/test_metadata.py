"""Domain metadata round-trip and ownership classification (design §8.4)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from hitkit.machine import metadata
from hitkit.machine.metadata import MachineMeta, Ownership

INSTALL_ID = "install-abc"


@pytest.fixture
def meta() -> MachineMeta:
    return MachineMeta(
        install_id=INSTALL_ID,
        kit="devbox",
        kit_version=1,
        machine="devbox",
        role="base",
        image="base-0123456789ab",
        address="10.77.0.40",
        created="2026-10-12T00:00:00Z",
        versions={"debian": "trixie", "systemd": "257", "openssh-server": "9.9p1"},
    )


def _xml(meta: MachineMeta) -> str:
    return ET.tostring(metadata.to_element(meta), encoding="unicode")


def test_round_trip_preserves_every_field_and_versions(meta):
    assert metadata.from_xml(_xml(meta)) == meta


def test_round_trip_with_no_versions(meta):
    bare = MachineMeta(**{**meta.__dict__, "versions": {}})
    assert metadata.from_xml(_xml(bare)) == bare


def test_classify_without_metadata_is_foreign():
    assert metadata.classify(None, INSTALL_ID) == (Ownership.FOREIGN, None)


def test_classify_malformed_xml_is_indeterminate():
    assert metadata.classify("<hitkit:machine", INSTALL_ID) == (Ownership.INDETERMINATE, None)


def test_classify_missing_field_is_indeterminate(meta):
    root = metadata.to_element(meta)
    root.remove(root.find(metadata._q("address")))
    xml = ET.tostring(root, encoding="unicode")
    assert metadata.classify(xml, INSTALL_ID) == (Ownership.INDETERMINATE, None)


def test_classify_other_install_is_indeterminate_and_keeps_meta(meta):
    status, found = metadata.classify(_xml(meta), "some-other-install")
    assert status is Ownership.INDETERMINATE
    assert found == meta


def test_classify_matching_install_is_owned(meta):
    assert metadata.classify(_xml(meta), INSTALL_ID) == (Ownership.OWNED, meta)


def test_from_xml_accepts_namespace_stripped_form_returned_by_libvirt():
    # virDomainGetMetadata returns the element without our namespace.
    meta = MachineMeta(
        install_id="abc", kit="devbox", kit_version=1, machine="devbox", role="base",
        image="base-1", address="10.77.0.40", created="2026-01-01T00:00:00+00:00",
        versions={"systemd": "257"},
    )
    namespaced = ET.tostring(metadata.to_element(meta), encoding="unicode")
    stripped = namespaced.replace("hitkit:", "").replace(' xmlns="urn:hitkit:metadata:1"', "")
    stripped = stripped.replace(' xmlns:hitkit="urn:hitkit:metadata:1"', "")
    assert "urn:hitkit" not in stripped
    assert metadata.from_xml(stripped) == meta
    assert metadata.classify(stripped, "abc")[0] is Ownership.OWNED
