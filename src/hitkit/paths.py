"""XDG locations for hitkit's host-side state (design §12).

`sudo hitkit setup` runs as root but must act on the invoking user's files, so
every function resolves the *target user* (``SUDO_USER`` when present) rather
than trusting ``$HOME``.
"""

from __future__ import annotations

import os
import pwd
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UserContext:
    name: str
    uid: int
    gid: int
    home: Path

    @property
    def config_dir(self) -> Path:
        return self._xdg("XDG_CONFIG_HOME", ".config") / "hitkit"

    @property
    def data_dir(self) -> Path:
        return self._xdg("XDG_DATA_HOME", ".local/share") / "hitkit"

    @property
    def cache_dir(self) -> Path:
        return self._xdg("XDG_CACHE_HOME", ".cache") / "hitkit"

    @property
    def state_dir(self) -> Path:
        return self._xdg("XDG_STATE_HOME", ".local/state") / "hitkit"

    @property
    def libvirt_session_dir(self) -> Path:
        """Where libvirt persists qemu:///session domain definitions (readable without libvirt)."""
        return self._xdg("XDG_CONFIG_HOME", ".config") / "libvirt" / "qemu"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def ca_dir(self) -> Path:
        return self.data_dir / "ca"

    def _xdg(self, var: str, default: str) -> Path:
        # Under sudo, the environment belongs to root; ignore XDG overrides.
        value = os.environ.get(var) if self.uid == os.geteuid() else None
        return Path(value) if value else self.home / default


def current_user() -> UserContext:
    """The user hitkit acts on behalf of: SUDO_USER under sudo, else the caller."""
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        pw = pwd.getpwnam(sudo_user)
    else:
        pw = pwd.getpwuid(os.geteuid())
    return UserContext(pw.pw_name, pw.pw_uid, pw.pw_gid, Path(pw.pw_dir))


def ensure_private_dir(path: Path, user: UserContext) -> Path:
    """Create ``path`` (and parents) as 0700, owned by ``user``."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    if os.geteuid() == 0:
        os.chown(path, user.uid, user.gid)
    return path


def write_private(path: Path, data: bytes, user: UserContext, mode: int = 0o600) -> None:
    """Atomically write ``data`` with ``mode``, owned by ``user``."""
    ensure_private_dir(path.parent, user)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.geteuid() == 0:
        os.chown(tmp, user.uid, user.gid)
    os.replace(tmp, path)
