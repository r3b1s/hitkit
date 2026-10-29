"""Client certificates in the user's NSS databases, for browser logins (no root needed).

A login certificate is an identity with a private key, not a trust anchor, so the
system trust store (trust.py) cannot hold it. Firefox keeps an NSS database per profile;
Chromium (and other NSS applications) share ~/.pki/nssdb. `pk12util` and `certutil` (NSS
tools) import into them without a browser running its import dialog.

The bundle's password reaches pk12util through a pipe (``-w /dev/fd/N``), never argv.
A database protected by a primary password fails cleanly (``-k /dev/null`` is an empty
one); such a database is reported, and the browser's own import dialog is the way in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hitkit import trace
from hitkit.errors import HitkitError

# Where Firefox keeps its profiles: classic, XDG (Firefox 147+), Flatpak and Snap.
FIREFOX_ROOTS = (".mozilla/firefox", ".config/mozilla/firefox",
                 ".var/app/org.mozilla.firefox/.mozilla/firefox", "snap/firefox/common/.mozilla/firefox")
SHARED_DB = ".pki/nssdb"


def databases(home: Path) -> list[Path]:
    """Every NSS database (directory with cert9.db) a browser of this user reads."""
    found = [home / SHARED_DB] if (home / SHARED_DB / "cert9.db").is_file() else []
    for root in FIREFOX_ROOTS:
        found += sorted(p.parent for p in (home / root).glob("*/cert9.db"))
    return found


def _tools() -> None:
    for tool in ("pk12util", "certutil"):
        if shutil.which(tool) is None:
            raise HitkitError(f"{tool} is not installed", hint="install your distribution's NSS tools "
                              "(Arch: nss; Debian: libnss3-tools; Fedora: nss-tools)")


def _installed(db: Path, nickname: str) -> list[str]:
    """The certificates (PEM) stored under ``nickname``."""
    out = trace.run(["certutil", "-L", "-d", f"sql:{db}", "-n", nickname, "-a"],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        return []
    pems = out.stdout.split("-----END CERTIFICATE-----")
    return [p.strip() + "\n-----END CERTIFICATE-----\n" for p in pems if "BEGIN CERTIFICATE" in p]


def _same(a: str, b: str) -> bool:
    return "".join(a.split()) == "".join(b.split())


def remove(db: Path, nickname: str) -> bool:
    """Delete every certificate (and its key) stored under ``nickname``. True if any was."""
    _tools()
    removed = False
    for _ in range(len(_installed(db, nickname))):
        out = trace.run(["certutil", "-F", "-d", f"sql:{db}", "-n", nickname, "-f", "/dev/null"],
                        capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if out.returncode != 0:
            raise HitkitError(f"could not remove {nickname!r} from {db}: {out.stderr.strip()}")
        removed = True
    return removed


def import_bundle(db: Path, bundle: Path, password: str, nickname: str, certificate: str) -> str:
    """Import a PKCS#12 bundle whose certificate is ``certificate`` (PEM) and whose friendly
    name is ``nickname``. Returns "imported", "replaced" (an older one went) or "present"."""
    _tools()
    installed = _installed(db, nickname)
    if len(installed) == 1 and _same(installed[0], certificate):
        return "present"
    replaced = remove(db, nickname)
    read, write = os.pipe()
    try:
        os.write(write, password.encode())
        os.close(write)
        out = trace.run(["pk12util", "-i", str(bundle), "-d", f"sql:{db}", "-k", "/dev/null",
                         "-w", f"/dev/fd/{read}"],
                        capture_output=True, text=True, stdin=subprocess.DEVNULL, pass_fds=(read,))
    finally:
        os.close(read)
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip().splitlines()
        if any("SEC_ERROR_BAD_PASSWORD" in line and "PKCS11 slot" in line for line in detail):
            raise HitkitError(f"{db} is protected by a primary password",
                              hint="import the certificate with the browser's own dialog instead")
        raise HitkitError(f"importing into {db} failed: {detail[-1] if detail else out.returncode}")
    return "replaced" if replaced else "imported"
