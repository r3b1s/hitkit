"""Thin access to libvirt. Imported lazily so pure modules and tests never need it."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from hitkit import trace
from hitkit.errors import HitkitError

_t = trace.logger(__name__)

SYSTEM_URI = "qemu:///system"
SESSION_URI = "qemu:///session"

_quiet = False


def _libvirt():
    global _quiet
    import libvirt

    if not _quiet:
        # libvirt prints every error to stderr by default; we report our own.
        libvirt.registerErrorHandler(lambda _ctx, _err: None, None)
        _quiet = True
    return libvirt


@contextmanager
def connect(uri: str, readonly: bool = False) -> Iterator["object"]:
    import os

    libvirt = _libvirt()
    if uri == SESSION_URI:  # before libvirt auto-spawns a daemon inside *our* cgroup
        from hitkit.host import session
        from hitkit.paths import current_user

        session.ensure(current_user())
    try:
        try:
            conn = libvirt.openReadOnly(uri) if readonly else libvirt.open(uri)
        except libvirt.libvirtError:
            if not (readonly and os.geteuid() == 0):
                raise
            _t.debug("read-only libvirt socket unavailable; using read-write as root", uri=uri)
            conn = libvirt.open(uri)  # root needs no read-only socket (often not enabled)
    except libvirt.libvirtError as e:
        _t.warning("libvirt connect failed", uri=uri, readonly=readonly, error=e.get_error_message())
        raise HitkitError(
            f"cannot connect to libvirt at {uri}: {e.get_error_message()}",
            hint="enable libvirt's sockets: systemctl enable --now libvirtd.socket libvirtd-ro.socket "
                 "(or, with modular daemons, virtqemud.socket virtnetworkd.socket and their -ro sockets)",
        ) from e
    _t.debug("libvirt connected", uri=uri, readonly=readonly)
    try:
        yield conn
    finally:
        conn.close()


def lookup_network(conn, name: str):
    """Return the network or None if it does not exist."""
    libvirt = _libvirt()
    try:
        return conn.networkLookupByName(name)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_NETWORK:
            return None
        raise
