import ipaddress
import stat

import pytest

from hitkit.host.managed_files import (
    BEGIN,
    END,
    apply_hosts_block,
    ensure_bridge_allow,
    has_bridge_allow,
    remove_bridge_allow,
    remove_hosts_block,
    write_system_file,
)

RECORDS = [
    (ipaddress.IPv4Address("10.77.0.10"), "forgejo.hitkit.internal"),
    (ipaddress.IPv4Address("10.77.0.20"), "runner-1.hitkit.internal"),
]
OTHER_RECORDS = [(ipaddress.IPv4Address("10.88.0.10"), "forgejo.hitkit.internal")]
HOSTS = "127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost\n"


def test_apply_appends_block_bounded_by_markers():
    out = apply_hosts_block(HOSTS, RECORDS)
    assert out.startswith(HOSTS)
    lines = out.splitlines()
    begin, end = lines.index(BEGIN), lines.index(END)
    assert end == len(lines) - 1
    assert lines[begin + 1 : end] == [
        "10.77.0.10\tforgejo.hitkit.internal",
        "10.77.0.20\trunner-1.hitkit.internal",
    ]


def test_apply_twice_is_idempotent():
    once = apply_hosts_block(HOSTS, RECORDS)
    assert apply_hosts_block(once, RECORDS) == once


def test_apply_with_new_records_replaces_block():
    first = apply_hosts_block(HOSTS, RECORDS)
    second = apply_hosts_block(first, OTHER_RECORDS)
    assert second.count(BEGIN) == 1
    assert second.count(END) == 1
    assert "10.77.0.10" not in second
    assert second == apply_hosts_block(HOSTS, OTHER_RECORDS)


def test_remove_restores_original_text_exactly():
    assert remove_hosts_block(apply_hosts_block(HOSTS, RECORDS)) == HOSTS


def test_apply_handles_missing_trailing_newline():
    text = "127.0.0.1\tlocalhost"
    out = apply_hosts_block(text, RECORDS)
    assert out.startswith("127.0.0.1\tlocalhost\n" + BEGIN + "\n")
    assert out.endswith(END + "\n")
    # The original line comes back newline-terminated; the block is fully gone.
    assert remove_hosts_block(out) == "127.0.0.1\tlocalhost\n"


def test_begin_without_end_raises():
    text = "127.0.0.1\tlocalhost\n" + BEGIN + "\n10.77.0.10\tforgejo.hitkit.internal\n"
    with pytest.raises(ValueError, match="END"):
        remove_hosts_block(text)
    with pytest.raises(ValueError):
        apply_hosts_block(text, RECORDS)


def test_ensure_bridge_allow_adds_line_once():
    once = ensure_bridge_allow("allow virbr0\n", "hitkit0")
    assert once == "allow virbr0\nallow hitkit0\n"
    assert ensure_bridge_allow(once, "hitkit0") == once
    assert has_bridge_allow(once, "hitkit0")


def test_ensure_bridge_allow_handles_missing_trailing_newline():
    assert ensure_bridge_allow("allow virbr0", "hitkit0") == "allow virbr0\nallow hitkit0\n"


def test_has_bridge_allow_false_when_absent():
    assert not has_bridge_allow("allow virbr0\n", "hitkit0")


def test_remove_bridge_allow_removes_only_hitkit():
    text = ensure_bridge_allow("allow virbr0\n", "hitkit0")
    out = remove_bridge_allow(text, "hitkit0")
    assert out == "allow virbr0\n"
    assert not has_bridge_allow(out, "hitkit0")


def test_write_system_file_preserves_existing_mode(tmp_path):
    target = tmp_path / "bridge.conf"
    target.write_text("allow virbr0\n")
    target.chmod(0o640)
    write_system_file(target, "allow virbr0\nallow hitkit0\n")
    assert target.read_text() == "allow virbr0\nallow hitkit0\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not (tmp_path / ".bridge.conf.hitkit-tmp").exists()


def test_write_system_file_new_file_defaults_to_0644(tmp_path):
    target = tmp_path / "etc" / "hosts"
    write_system_file(target, "127.0.0.1\tlocalhost\n")
    assert target.read_text() == "127.0.0.1\tlocalhost\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
