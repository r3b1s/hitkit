"""Host-side secret store: plain 0600 files per kit (design §5, accepted risk).

Anything able to read these already runs as the user, so encryption at rest
would add friction (notably for agents) without changing the threat model.
"""

from __future__ import annotations

import base64
import secrets as _secrets
import shutil
from pathlib import Path
from typing import Callable

from hitkit import trace
from hitkit.paths import UserContext, write_private

# Bookkeeping files in the store that are not secret; everything else is masked in traces.
_PLAIN = (".json", ".pub")


def _register(name: str, value: str | None) -> str | None:
    if value is not None and not name.endswith(_PLAIN):
        trace.secret(value)
    return value


def kit_dir(user: UserContext, kit: str) -> Path:
    return user.data_dir / "secrets" / kit


def get(user: UserContext, kit: str, name: str) -> str | None:
    try:
        return _register(name, (kit_dir(user, kit) / name).read_text())
    except FileNotFoundError:
        return None


def put(user: UserContext, kit: str, name: str, value: str) -> None:
    _register(name, value)
    write_private(kit_dir(user, kit) / name, value.encode(), user)


def get_or_create(user: UserContext, kit: str, name: str, generate: Callable[[], str]) -> str:
    value = get(user, kit, name)
    if value is None:
        value = generate()
        put(user, kit, name, value)
    return value


def delete(user: UserContext, kit: str, name: str) -> None:
    (kit_dir(user, kit) / name).unlink(missing_ok=True)


def remove_kit(user: UserContext, kit: str) -> None:
    shutil.rmtree(kit_dir(user, kit), ignore_errors=True)


# --- generators ------------------------------------------------------------


def hex_secret(nbytes: int) -> Callable[[], str]:
    return lambda: _secrets.token_hex(nbytes)


def password() -> str:
    return _secrets.token_urlsafe(24)


def b64url_32() -> str:
    """32 random bytes, unpadded base64url (the format Forgejo's JWT secrets use)."""
    return base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode()
