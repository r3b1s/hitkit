"""User-side SSH config: the marked Include block, the snippet and the pinned known_hosts."""

from __future__ import annotations

from pathlib import Path

from hitkit import sshconfig

SNIPPET = Path("/home/t/.config/hitkit/ssh_config")
EXISTING = "Host other\n    HostName example.org\n"


def test_with_include_puts_the_block_first():
    text = sshconfig.with_include(EXISTING, SNIPPET)

    assert text.startswith(f"{sshconfig.BEGIN}\nInclude {SNIPPET}\n{sshconfig.END}\n")
    assert text.endswith(EXISTING)


def test_with_include_applied_twice_does_not_duplicate_the_block():
    once = sshconfig.with_include(EXISTING, SNIPPET)
    assert sshconfig.with_include(once, SNIPPET) == once


def test_remove_block_restores_the_original_text():
    assert sshconfig.remove_block(sshconfig.with_include(EXISTING, SNIPPET)) == EXISTING


def test_install_writes_snippet_known_hosts_and_include(user):
    config = sshconfig.user_ssh_config(user)
    config.parent.mkdir(parents=True)
    config.write_text(EXISTING)

    sshconfig.install(user, "ssh-ed25519 AAAA host")

    snippet = sshconfig.snippet_path(user).read_text()
    key = sshconfig.key_path(user)
    assert "Host forgejo.hitkit.internal" in snippet
    assert "Port 2222" in snippet
    assert "User git" in snippet
    assert f"IdentityFile {key}" in snippet
    assert "StrictHostKeyChecking yes" in snippet

    known_hosts = sshconfig.known_hosts_path(user).read_text()
    assert known_hosts == "[forgejo.hitkit.internal]:2222 ssh-ed25519 AAAA host\n"

    text = config.read_text()
    assert text.startswith(f"{sshconfig.BEGIN}\nInclude {sshconfig.snippet_path(user)}\n")
    assert text.endswith(EXISTING)


def test_install_into_missing_ssh_config_creates_it(user):
    sshconfig.install(user, "ssh-ed25519 AAAA host")
    assert sshconfig.user_ssh_config(user).read_text().count(sshconfig.BEGIN) == 1


def test_uninstall_removes_block_snippet_and_known_hosts_but_keeps_other_config(user):
    config = sshconfig.user_ssh_config(user)
    config.parent.mkdir(parents=True)
    config.write_text(EXISTING)
    sshconfig.install(user, "ssh-ed25519 AAAA host")

    sshconfig.uninstall(user)

    assert config.read_text() == EXISTING
    assert not sshconfig.snippet_path(user).exists()
    assert not sshconfig.known_hosts_path(user).exists()
