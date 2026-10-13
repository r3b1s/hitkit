"""Kits: curated environments composed of machines (design §9).

Kits ship inside hitkit only. Each is a directory with a ``kit.toml`` and an
optional ``hooks.py`` for post-boot wiring. Knobs may change sizing and toggle
features; they may never change topology (counts of identical machines are
sizing).
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from hitkit.errors import HitkitError
from hitkit.machine.spec import MachineSpec

KITS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Knob:
    name: str
    type: str  # "int" | "bool"
    default: Any
    help: str = ""
    min: int | None = None
    max: int | None = None

    def coerce(self, raw: str | bool) -> Any:
        if self.type == "bool":
            return bool(raw)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HitkitError(f"--{self.name} expects an integer, got {raw!r}") from None
        if (self.min is not None and value < self.min) or (self.max is not None and value > self.max):
            raise HitkitError(f"--{self.name} must be between {self.min} and {self.max}")
        return value


def _parse_bool(name: str, raw: str) -> bool:
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise HitkitError(f"--{name} expects true or false, got {raw!r}")


@dataclass(frozen=True)
class MachineTemplate:
    spec: MachineSpec  # for scaled machines: the first instance (e.g. runner-1)
    count_knob: str | None = None
    prefix: str | None = None  # scaled machines are named <prefix>-<n>


@dataclass(frozen=True)
class Kit:
    name: str
    description: str
    version: int
    templates: tuple[MachineTemplate, ...]
    knobs: dict[str, Knob] = field(default_factory=dict)

    @property
    def machines(self) -> tuple[MachineSpec, ...]:
        """Machines with default knob values."""
        return self.expand(self.default_knobs())

    def default_knobs(self) -> dict[str, Any]:
        return {k.name: k.default for k in self.knobs.values()}

    def expand(self, knobs: dict[str, Any]) -> tuple[MachineSpec, ...]:
        specs: list[MachineSpec] = []
        for t in self.templates:
            if t.count_knob is None:
                specs.append(t.spec)
            else:
                specs += [replace(t.spec, name=f"{t.prefix}-{i}") for i in range(1, knobs[t.count_knob] + 1)]
        return tuple(specs)

    def parse_knobs(self, args: list[str], previous: dict[str, Any] | None = None) -> dict[str, Any]:
        """Parse `--name value`, `--name=value`, `--flag`, `--no-flag` against declared knobs.
        Knobs not given keep their ``previous`` value (from the last `up`), else the default."""
        values = self.default_knobs() | {k: v for k, v in (previous or {}).items() if k in self.knobs}
        it = iter(args)
        for arg in it:
            if not arg.startswith("--"):
                raise HitkitError(f"unexpected argument {arg!r}")
            key, has_inline, inline = arg[2:].partition("=")
            negated = key.startswith("no-") and key[3:] in self.knobs
            name = key[3:] if negated else key
            knob = self.knobs.get(name)
            if knob is None:
                known = ", ".join(f"--{k}" for k in self.knobs) or "none"
                raise HitkitError(f"kit {self.name!r} has no knob --{name}", hint=f"knobs: {known}")
            if knob.type == "bool":
                if negated and has_inline:
                    raise HitkitError(f"--no-{name} takes no value")
                values[name] = _parse_bool(name, inline) if has_inline else not negated
            elif negated:
                raise HitkitError(f"--{name} is not a flag; --no-{name} is invalid")
            else:
                values[name] = knob.coerce(inline if has_inline else next(it, None))
        return values

    def spec_for(self, machine: str) -> MachineSpec | None:
        """The (default-sized) spec a machine of this kit was made from, scaled ones included."""
        for t in self.templates:
            if t.spec.name == machine or (t.prefix and machine.rsplit("-", 1)[0] == t.prefix):
                return replace(t.spec, name=machine)
        return None

    @property
    def selectors(self) -> set[str]:
        """Names a sizing flag can target: fixed machines by name, scaled ones by prefix."""
        return {t.prefix or t.spec.name for t in self.templates}


# --- sizing: --cpus / --memory / --disk ----------------------------------------------

SIZING = {"cpus": (1, 64, "--cpus"), "memory_mib": (512, 262144, "--memory"), "data_disk_gib": (1, 65536, "--disk")}


def parse_sizing(kit: Kit, given: dict[str, tuple[str, ...]], previous: dict | None = None) -> dict:
    """Merge sizing flags into the sizing remembered from earlier `up`s.

    Each value is `N` (every machine of the kit; replaces earlier per-machine values for
    that dimension) or `MACHINE=N` (one machine, or every instance of a scaled one)."""
    sizing = {dim: dict(v) for dim, v in (previous or {}).items() if dim in SIZING}
    for dim, raws in given.items():
        low, high, flag = SIZING[dim]
        for raw in raws:
            target, _, value = raw.rpartition("=")
            try:
                n = int(value)
            except ValueError:
                raise HitkitError(f"{flag} expects N or MACHINE=N, got {raw!r}") from None
            if not low <= n <= high:
                raise HitkitError(f"{flag} {raw}: must be between {low} and {high}")
            if not target:
                sizing[dim] = {"*": n}
            elif target in kit.selectors:
                sizing.setdefault(dim, {})[target] = n
            else:
                raise HitkitError(f"kit {kit.name!r} has no machine {target!r}",
                                  hint=f"machines: {', '.join(sorted(kit.selectors))}")
    return sizing


def apply_sizing(kit: Kit, plan: tuple[MachineSpec, ...], sizing: dict | None) -> tuple[MachineSpec, ...]:
    """Specs with sizing applied: the machine's own value, else its scaled prefix's, else `*`."""
    if not sizing:
        return plan
    prefixes = {t.prefix for t in kit.templates if t.prefix}

    def pick(spec: MachineSpec, dim: str):
        values = sizing.get(dim, {})
        prefix = spec.name.rsplit("-", 1)[0]
        for key in (spec.name, prefix if prefix in prefixes else None, "*"):
            if key in values:
                return values[key]
        return getattr(spec, dim)

    return tuple(replace(s, **{dim: pick(s, dim) for dim in SIZING}) for s in plan)


class Hooks:
    """Kit-specific wiring. Kits override what they need in ``hooks.py``."""

    def credentials(self, hctx, machine: MachineSpec, plan: tuple[MachineSpec, ...]) -> dict[str, bytes]:
        """Extra credentials (name -> content) for a machine, merged into the common set."""
        return {}

    def after_ready(self, hctx, machine: MachineSpec, plan: tuple[MachineSpec, ...], knobs: dict, log) -> None:
        """Called after each machine reports ready, in kit order."""

    def finish(self, hctx, plan: tuple[MachineSpec, ...], knobs: dict, log) -> None:
        """Called once every machine is ready."""

    def creds(self, hctx) -> dict:
        """Human-facing URLs and logins for `hitkit creds`."""
        return {}

    def backup_plan(self, machine):
        """How `hitkit backup` copies a machine's /srv/data (a MachineInfo); None skips it.
        Default: offline, i.e. the machine is stopped and its data disk copied exactly."""
        from hitkit.backup import BackupPlan

        return BackupPlan()


def parse(text: str) -> Kit:
    raw = tomllib.loads(text)
    name, version = raw["name"], int(raw["version"])
    knobs = {
        k: Knob(name=k, type=v["type"], default=v["default"], help=v.get("help", ""),
                min=v.get("min"), max=v.get("max"))
        for k, v in raw.get("knobs", {}).items()
    }
    templates = []
    for m in raw["machine"]:
        count_knob = m.get("count_knob")
        if count_knob is not None and count_knob not in knobs:
            raise ValueError(f"machine {m['name']!r} uses undeclared knob {count_knob!r}")
        # Scaled machines are validated per instance (runner-1, ...) when expanded.
        spec_name = f"{m['name']}-1" if count_knob else m["name"]
        spec = MachineSpec(
            name=spec_name, role=m["role"], cpus=int(m["cpus"]), memory_mib=int(m["memory_mib"]),
            data_disk_gib=int(m["data_disk_gib"]), kit=name, kit_version=version,
            stateful_os=bool(m.get("stateful_os", False)), ready_timeout=int(m.get("ready_timeout", 300)),
        )
        templates.append(MachineTemplate(spec, count_knob, m["name"] if count_knob else None))
    return Kit(name, raw["description"], version, tuple(templates), knobs)


def all_kits() -> list[Kit]:
    return [parse(p.read_text()) for p in sorted(KITS_DIR.glob("*/kit.toml"))]


def load(name: str) -> Kit:
    path = KITS_DIR / name / "kit.toml"
    if not path.is_file():
        known = ", ".join(k.name for k in all_kits())
        raise HitkitError(f"unknown kit {name!r}", hint=f"available kits: {known}")
    return parse(path.read_text())


def hooks(name: str) -> Hooks:
    kit_toml = KITS_DIR / name / "kit.toml"
    if kit_toml.is_file() and (shared := tomllib.loads(kit_toml.read_text()).get("hooks_from")):
        name = shared  # e.g. the per-distro dev kits reuse hitkit-dev's hooks
    path = KITS_DIR / name / "hooks.py"
    if not path.is_file():
        return Hooks()
    module_name = f"hitkit.kits._hooks_{name.replace('-', '_')}"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    module = sys.modules[module_name]
    return module.KitHooks()
