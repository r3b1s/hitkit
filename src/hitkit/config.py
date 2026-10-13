"""Host-level hitkit configuration: just the subnet and the install ID.

Kept deliberately tiny. libvirt is the source of truth for what exists; this
file only records the two facts libvirt cannot: which subnet the user chose and
which install of hitkit owns the domains (design §8.4).

Losing the install ID would orphan every machine, so it is never minted again
while a copy survives (design §13.13). Recovery order: config.toml, the copy in
the data directory, then the hitkit metadata in libvirt's saved domain
definitions. A new ID is minted only when there are no hitkit machines at all.
"""

from __future__ import annotations

import ipaddress
import tomllib
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from hitkit.addressing import DEFAULT_SUBNET, parse_subnet
from hitkit.errors import Conflict
from hitkit.paths import UserContext, write_private

METADATA_NS = "urn:hitkit:metadata:1"  # hitkit.machine.metadata.NS (not imported: keeps this module light)


@dataclass(frozen=True)
class HostConfig:
    subnet: str
    install_id: str

    def network(self):
        return parse_subnet(self.subnet)


def _copy_path(user: UserContext):
    return user.data_dir / "install.toml"


def _parse(text: str) -> HostConfig:
    raw = tomllib.loads(text)
    return HostConfig(subnet=raw["subnet"], install_id=raw["install_id"])


@dataclass
class Install:
    """One install ID found in libvirt's saved domain definitions."""
    install_id: str
    machines: list[str] = field(default_factory=list)
    subnet: str | None = None


def discover(user: UserContext) -> dict[str, Install]:
    """Install IDs named by hitkit domains, from the XML libvirt keeps on disk. Works under
    sudo (root cannot reach the user's qemu:///session) and needs no running libvirt."""
    found: dict[str, Install] = {}
    directory = user.libvirt_session_dir
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.xml")):
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        machine = root.find(f".//{{{METADATA_NS}}}machine")
        iid = machine.findtext(f"{{{METADATA_NS}}}install-id") if machine is not None else None
        if not iid:
            continue
        entry = found.setdefault(iid, Install(iid))
        entry.machines.append(machine.findtext(f"{{{METADATA_NS}}}machine") or path.stem)
        address = machine.findtext(f"{{{METADATA_NS}}}address")
        if address and entry.subnet is None:
            try:
                entry.subnet = str(ipaddress.ip_network(f"{address}/24", strict=False))
            except ValueError:
                pass
    return found


def _recover(user: UserContext) -> HostConfig | None:
    try:
        return _parse(_copy_path(user).read_text())
    except FileNotFoundError:
        pass
    installs = discover(user)
    if len(installs) > 1:
        summary = "; ".join(f"{i.install_id} ({', '.join(i.machines)})" for i in installs.values())
        raise Conflict(f"hitkit's config is missing and machines belong to several install IDs: {summary}",
                       hint="choose one: sudo hitkit setup --install-id <ID>")
    if installs:
        found = next(iter(installs.values()))
        return HostConfig(subnet=found.subnet or DEFAULT_SUBNET, install_id=found.install_id)
    return None


def load(user: UserContext, persist: bool = True) -> HostConfig | None:
    """The host config, recovering (and, if ``persist``, re-saving) it if config.toml is gone
    but a copy survives."""
    try:
        cfg = _parse(user.config_file.read_text())
    except FileNotFoundError:
        cfg = None
    if cfg is not None:
        if persist and not _copy_path(user).exists():
            try:
                save(user, cfg)  # installs from before the copy existed get one now
            except OSError:
                pass
        return cfg
    recovered = _recover(user)
    if recovered is not None:
        from hitkit import trace

        import sys

        trace.logger(__name__).warning("config.toml was missing; recovered the install ID",
                                       install_id=recovered.install_id, subnet=recovered.subnet)
        print(f"hitkit: {user.config_file} was missing; recovered install ID {recovered.install_id} "
              f"(subnet {recovered.subnet})", file=sys.stderr)
        if persist:
            save(user, recovered)
    return recovered


def save(user: UserContext, cfg: HostConfig) -> None:
    parse_subnet(cfg.subnet)
    text = f'subnet = "{cfg.subnet}"\ninstall_id = "{cfg.install_id}"\n'
    write_private(user.config_file, text.encode(), user)
    write_private(_copy_path(user), text.encode(), user)  # the second copy recovery relies on


def load_or_create(user: UserContext, subnet: str | None = None, install_id: str | None = None) -> HostConfig:
    """Load config, creating it on first use. ``subnet`` overrides on create or change.

    ``install_id`` adopts a specific ID, which is how a user resolves the ambiguous case
    `discover` refuses to guess about; it may not replace an ID already configured."""
    existing = None if install_id and not user.config_file.exists() else load(user)
    if install_id and existing and existing.install_id != install_id:
        raise Conflict(f"hitkit is already configured with install ID {existing.install_id}",
                       hint="--install-id only applies when the config is missing")
    if existing and (subnet is None or subnet == existing.subnet):
        return existing
    found = discover(user).get(install_id) if install_id else None
    cfg = HostConfig(
        subnet=subnet or (existing.subnet if existing else (found and found.subnet) or DEFAULT_SUBNET),
        install_id=existing.install_id if existing else install_id or uuid.uuid4().hex,
    )
    save(user, cfg)
    return cfg
