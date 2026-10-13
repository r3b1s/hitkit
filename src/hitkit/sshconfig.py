"""User-side SSH configuration for git over SSH to the forge.

hitkit writes its own snippet (~/.config/hitkit/ssh_config) with a dedicated
key and a pinned known_hosts file, and includes it from ~/.ssh/config in a
marked block that `hitkit destroy forge` removes again.
"""

from __future__ import annotations

from pathlib import Path

from hitkit.paths import UserContext, ensure_private_dir, write_private

BEGIN = "# BEGIN hitkit — managed by `hitkit up forge`; removed by `hitkit destroy forge`"
END = "# END hitkit"
FORGE_HOST = "forgejo.hitkit.internal"
FORGE_SSH_PORT = 2222


def key_path(user: UserContext) -> Path:
    return user.data_dir / "ssh" / "forge_ed25519"


def known_hosts_path(user: UserContext) -> Path:
    return user.data_dir / "ssh" / "forge_known_hosts"


def snippet_path(user: UserContext) -> Path:
    return user.config_dir / "ssh_config"


def user_ssh_config(user: UserContext) -> Path:
    return user.home / ".ssh" / "config"


def snippet(user: UserContext) -> str:
    return (
        "# Managed by hitkit: git over SSH to the local forge.\n"
        f"Host {FORGE_HOST}\n"
        f"    Port {FORGE_SSH_PORT}\n"
        "    User git\n"
        f"    IdentityFile {key_path(user)}\n"
        "    IdentitiesOnly yes\n"
        f"    UserKnownHostsFile {known_hosts_path(user)}\n"
        "    StrictHostKeyChecking yes\n"
    )


def known_hosts_line(host_public_key: str) -> str:
    return f"[{FORGE_HOST}]:{FORGE_SSH_PORT} {host_public_key}\n"


def remove_block(text: str) -> str:
    out, inside = [], False
    for line in text.splitlines(keepends=True):
        if line.rstrip("\n") == BEGIN:
            inside = True
        elif inside and line.rstrip("\n") == END:
            inside = False
        elif not inside:
            out.append(line)
    return "".join(out)


def with_include(text: str, snippet_file: Path) -> str:
    """Put the Include block first: an Include before any Host block applies globally."""
    block = f"{BEGIN}\nInclude {snippet_file}\n{END}\n"
    return block + remove_block(text)


def install(user: UserContext, host_public_key: str) -> None:
    ensure_private_dir(known_hosts_path(user).parent, user)
    write_private(known_hosts_path(user), known_hosts_line(host_public_key).encode(), user, mode=0o644)
    write_private(snippet_path(user), snippet(user).encode(), user, mode=0o644)
    config = user_ssh_config(user)
    ensure_private_dir(config.parent, user)
    current = config.read_text() if config.exists() else ""
    desired = with_include(current, snippet_path(user))
    if desired != current:
        write_private(config, desired.encode(), user, mode=0o600)


def uninstall(user: UserContext) -> None:
    config = user_ssh_config(user)
    if config.exists():
        current = config.read_text()
        if (desired := remove_block(current)) != current:
            write_private(config, desired.encode(), user, mode=0o600)
    snippet_path(user).unlink(missing_ok=True)
    known_hosts_path(user).unlink(missing_ok=True)
