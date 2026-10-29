"""hitkit command line (design §10).

Conventions: every command takes the global --json flag, nothing prompts, and
exit codes come from hitkit.errors. Commands stay thin; logic lives in the
layers below.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path

import click

from hitkit import __version__, config
from hitkit.addressing import DEFAULT_SUBNET
from hitkit.errors import EXIT_CHECKS_FAILED, EXIT_ERROR, HitkitError
from hitkit.paths import current_user


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


_GLOBAL_FLAGS = ("--json", "--debug")


def _hoist_global_flags(args: list[str]) -> list[str]:
    """Accept --json/--debug anywhere (`hitkit status --json`), not only before the
    command. Arguments after `--`, and everything `ssh` passes to the remote, are left alone."""
    words = [a for a in args if not a.startswith("-")]
    if words[:1] == ["ssh"]:
        stop = args.index("ssh")
    else:
        stop = args.index("--") if "--" in args else len(args)
    flags = [a for a in args[:stop] if a in _GLOBAL_FLAGS]
    return flags + [a for i, a in enumerate(args) if i >= stop or a not in _GLOBAL_FLAGS]


class _Group(click.Group):
    """Traces every invocation and turns errors into a clean message, hint, trace id, and exit code."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ctx.meta["hitkit.argv"] = list(args)
        return super().parse_args(ctx, _hoist_global_flags(args))

    def invoke(self, ctx: click.Context):
        import logging
        import platform

        from hitkit import trace

        argv = ctx.meta.get("hitkit.argv", [])
        words = [a for a in argv if not a.startswith("-")]
        debug = bool(ctx.params.get("debug")) or _env_flag("HITKIT_DEBUG")
        ctx.meta["hitkit.debug"] = debug
        user = None if words[:1] == ["trace"] else current_user()  # reading traces is not traced
        quiet = words[:2] == ["pool", "run"] and not debug  # a long-running loop: no per-poll noise
        trace.setup(user, " ".join(a for a in argv if a not in ("--json", "--debug"))[:200], debug,
                    logging.INFO if quiet else logging.DEBUG)
        log = trace.logger("cli")
        log.info("command started", argv=argv, version=__version__, euid=os.geteuid(),
                 user=user.name if user else None, python=platform.python_version())
        started = time.monotonic()

        def finished(code: int, msg: str = "command finished", **fields) -> None:
            level = log.info if code == 0 else log.error
            level(msg, exit_code=code, duration_ms=round((time.monotonic() - started) * 1000), **fields)

        try:
            result = super().invoke(ctx)
        except HitkitError as e:
            finished(e.exit_code, "command failed", error=str(e), hint=e.hint, exc_info=True)
            self._report(ctx, str(e), e.hint)
            ctx.exit(e.exit_code)
        except click.exceptions.Exit as e:
            finished(e.exit_code)
            raise
        except click.ClickException as e:
            finished(e.exit_code, "command failed", error=e.format_message())
            raise
        except (click.Abort, KeyboardInterrupt):
            finished(130, "command interrupted")
            raise
        except Exception as e:
            if type(e).__name__ == "libvirtError":
                message, hint = f"libvirt: {e.get_error_message()}", None
            else:
                message = f"internal error: {type(e).__name__}: {e}"
                hint = "this is a bug in hitkit; the trace below has the full traceback"
            finished(EXIT_ERROR, "command failed", error=message, exc_info=True)
            self._report(ctx, message, hint)
            ctx.exit(EXIT_ERROR)
        finished(0)
        return result

    @staticmethod
    def _report(ctx: click.Context, message: str, hint: str | None) -> None:
        from hitkit import trace

        tid = trace.trace_id()
        if ctx.obj and ctx.obj.get("json"):
            click.echo(json.dumps({"error": message, "hint": hint, "trace": tid}))
        else:
            click.secho(f"error: {message}", fg="red", err=True)
            if hint:
                click.echo(f"hint: {hint}", err=True)
            click.echo(f"trace: {tid} (details: hitkit trace {tid})", err=True)


def _json(ctx: click.Context) -> bool:
    return bool(ctx.find_root().obj.get("json"))


def _emit(ctx: click.Context, data) -> None:
    click.echo(json.dumps(data, indent=2))


@click.group(cls=_Group)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--debug", is_flag=True, help="Also print the trace's debug records to stderr (or set HITKIT_DEBUG=1).")
@click.version_option(__version__, prog_name="hitkit")
@click.pass_context
def cli(ctx: click.Context, as_json: bool, debug: bool) -> None:
    """Opinionated self-hosted dev infrastructure on your own workstation."""
    ctx.obj = {"json": as_json}


# --- setup -----------------------------------------------------------------


def _run_plan(ctx: click.Context, changes, dry_run: bool) -> None:
    summaries = [c.summary for c in changes]
    if not _json(ctx):
        if not changes:
            click.echo("Nothing to do.")
        else:
            click.echo("Plan:")
            for s in summaries:
                click.echo(f"  • {s}")
    applied = []
    if not dry_run:
        for change in changes:
            change.apply()
            applied.append(change.summary)
            if not _json(ctx):
                click.secho(f"  ✓ {change.summary}", fg="green")
    if _json(ctx):
        _emit(ctx, {"plan": summaries, "applied": applied, "dry_run": dry_run})


@cli.command()
@click.option("--subnet", help=f"Private /24 for the hitkit network (default {DEFAULT_SUBNET}).")
@click.option("--trust", "trust_ca", is_flag=True, help="Also add the hitkit CA to the host trust store.")
@click.option("--force", is_flag=True, help="Replace a conflicting existing 'hitkit' network.")
@click.option("--remove", is_flag=True, help="Undo everything setup did (keeps your CA and config).")
@click.option("--dry-run", is_flag=True, help="Show the plan without changing anything.")
@click.option("--install-id", help="Adopt this install ID when the config is missing and machines name several.")
@click.pass_context
def setup(ctx, subnet, trust_ca, force, remove, dry_run, install_id) -> None:
    """One-time privileged host setup: network, DNS, bridge permission."""
    from hitkit.host import setup as setup_mod

    user = current_user()
    if not dry_run:
        setup_mod.require_root()
    if remove:
        changes = setup_mod.plan_remove(user)
    else:
        if dry_run:
            existing = config.load(user, persist=False)
            cfg = config.HostConfig(
                subnet=subnet or (existing.subnet if existing else DEFAULT_SUBNET),
                install_id=existing.install_id if existing else "0" * 32,
            )
        else:
            cfg = config.load_or_create(user, subnet, install_id)
        changes = setup_mod.plan_setup(user, cfg, trust_ca=trust_ca, force=force)
    _run_plan(ctx, changes, dry_run)


# --- trust -----------------------------------------------------------------


@cli.group()
def trust() -> None:
    """Manage the hitkit CA in the host trust store (requires root)."""


@trust.command("install")
@click.pass_context
def trust_install(ctx) -> None:
    from hitkit.host import setup as setup_mod

    setup_mod.require_root()
    user = current_user()
    cfg = config.load_or_create(user)
    _run_plan(ctx, setup_mod.plan_trust_install(user, cfg), dry_run=False)


@trust.command("remove")
@click.pass_context
def trust_remove(ctx) -> None:
    from hitkit.host import setup as setup_mod

    setup_mod.require_root()
    _run_plan(ctx, setup_mod.plan_trust_remove(current_user()), dry_run=False)


# --- doctor ----------------------------------------------------------------

_SYMBOL = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "fail": ("✗", "red"), "info": ("·", None)}


@cli.command()
@click.pass_context
def doctor(ctx) -> None:
    """Read-only host diagnostics."""
    from hitkit.host import doctor as doctor_mod

    user = current_user()
    results = doctor_mod.run_checks(user, config.load(user))
    failed = any(r.status == "fail" for r in results)
    if _json(ctx):
        _emit(ctx, {"ok": not failed, "checks": [asdict(r) for r in results]})
    else:
        for r in results:
            symbol, color = _SYMBOL[r.status]
            click.echo(f"{click.style(symbol, fg=color)} {r.name:<18} {r.message}")
            if r.hint and r.status in ("warn", "fail"):
                click.echo(f"  {'':<18} hint: {r.hint}")
    if failed:
        ctx.exit(EXIT_CHECKS_FAILED)


# --- machines and kits -----------------------------------------------------


def _log(ctx: click.Context):
    """Progress messages go to stderr so --json stdout stays parseable, and into the trace."""
    from hitkit import trace

    progress = trace.logger("progress")

    def log(msg: str) -> None:
        progress.info(msg)
        if not ctx.find_root().meta.get("hitkit.debug"):  # --debug already mirrors the record
            click.echo(f"› {msg}", err=True)

    return log


def _host_context():
    from hitkit.errors import HitkitError
    from hitkit.host import ca as ca_mod
    from hitkit.machine.lifecycle import HostContext

    user = current_user()
    cfg = config.load(user)
    if cfg is None:
        raise HitkitError("hitkit is not set up on this host", hint="sudo hitkit setup")
    return HostContext(user, cfg, ca_mod.ensure_ca(user, cfg.network(), cfg.install_id))


def _session():
    from hitkit import virt

    return virt.connect(virt.SESSION_URI)


def _machine_row(info, hctx=None) -> dict:
    from hitkit.machine import lifecycle

    meta = info.meta
    stateful = _stateful(meta.kit if meta else None, info.name)
    return {
        "outdated": bool(hctx and not stateful and lifecycle.outdated(hctx, info)),
        "stateful_os": stateful,
        "name": info.name,
        "kit": meta.kit if meta else None,
        "state": info.state,
        "address": meta.address if meta else None,
        "fqdn": f"{info.name}.hitkit.internal",
        "image": meta.image if meta else None,
        "ownership": info.ownership.value,
        "versions": meta.versions if meta else {},
        "cpus": info.cpus,
        "memory_mib": info.memory_mib,
        **(_disk_usage(hctx, info.name) if hctx else {}),
    }


def _stateful(kit_name: str | None, machine: str) -> bool:
    """Whether a machine's OS disk holds state (from its kit's definition)."""
    from hitkit import kits

    candidates = [kits.load(kit_name)] if kit_name else kits.all_kits()
    for kit in candidates:
        spec = kit.spec_for(machine)
        if spec is not None:
            return spec.stateful_os
    return False


def _disk_usage(hctx, name: str) -> dict:
    from hitkit.errors import HitkitError
    from hitkit.machine import disks

    disk = hctx.paths(name).data_disk
    try:
        d = disks.info(disk) if disk.exists() else None
    except HitkitError:
        d = None
    return {"disk_gib": round(d["virtual-size"] / 2**30) if d else None,
            "disk_used_gib": round(d["actual-size"] / 2**30, 1) if d else None}


def _print_rows(rows: list[dict]) -> None:
    if not rows:
        click.echo("No hitkit machines.")
        return
    click.echo(f"{'MACHINE':<12} {'KIT':<18} {'STATE':<9} {'ADDRESS':<13} {'SIZE':<20} {'IMAGE':<20} OWNER")
    for r in rows:
        image = (r["image"] or "-") + (" (outdated)" if r.get("outdated") else "")
        size = "-"
        if r.get("cpus"):
            size = f"{r['cpus']}c {r['memory_mib'] / 1024:g}G"
            if r.get("disk_gib") is not None:
                size += f" {r['disk_used_gib']:g}/{r['disk_gib']}G"
        click.echo(
            f"{r['name']:<12} {r['kit'] or '-':<18} {r['state']:<9} {r['address'] or '-':<13} "
            f"{size:<20} {image:<20} {r['ownership']}"
        )


@cli.command("kits")
@click.pass_context
def kits_cmd(ctx) -> None:
    """List available kits and their machines."""
    from hitkit import kits

    data = [
        {
            "name": k.name,
            "description": k.description,
            "machines": [
                {"name": m.name, "role": m.role, "cpus": m.cpus, "memory_mib": m.memory_mib,
                 "data_disk_gib": m.data_disk_gib}
                for m in k.machines
            ],
            "knobs": {n: {"type": kn.type, "default": kn.default, "help": kn.help} for n, kn in k.knobs.items()},
        }
        for k in kits.all_kits()
    ]
    if _json(ctx):
        _emit(ctx, data)
        return
    for k in data:
        click.echo(f"{k['name']}: {k['description']}")
        for m in k["machines"]:
            click.echo(f"  {m['name']:<10} {m['cpus']} vCPU, {m['memory_mib']} MiB, {m['data_disk_gib']} GiB data")
        for n, kn in k["knobs"].items():
            flag = f"--[no-]{n}" if kn["type"] == "bool" else f"--{n} N"
            click.echo(f"  knob {flag:<16} {kn['help']} (default {kn['default']})")


@cli.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.argument("kit_name")
@click.option("--cpus", multiple=True, metavar="[MACHINE=]N", help="vCPUs: every machine, or MACHINE=N. Repeatable.")
@click.option("--memory", multiple=True, metavar="[MACHINE=]MIB", help="Memory in MiB: every machine, or MACHINE=MIB.")
@click.option("--disk", multiple=True, metavar="[MACHINE=]GIB",
              help="Data disk size in GiB: every machine, or MACHINE=GIB. Grows live; never shrinks.")
@click.pass_context
def up(ctx, kit_name, cpus, memory, disk) -> None:
    """Create or start a kit's machines, wire them, and wait until ready. Idempotent.

    Kit-specific knobs follow the kit name, e.g. `hitkit up forge --runners 2 --no-renovate`
    (see `hitkit kits`). Sizing is remembered: `hitkit up proxmox --disk 500` once, and later
    `hitkit up proxmox` keeps 500 GiB. MACHINE names a machine, or a scaled one's prefix
    (`--disk runner=200` sizes every runner-N).
    """
    _bring_up(ctx, kit_name, ctx.args, {"cpus": cpus, "memory_mib": memory, "data_disk_gib": disk})


def _bring_up(ctx, kit_name: str, knob_args: list[str], sizing_args: dict) -> None:
    from hitkit import kits, secrets, trace
    from hitkit.machine import lifecycle

    kit = kits.load(kit_name)
    hctx = _host_context()
    previous = secrets.get(hctx.user, kit.name, "knobs.json")
    knobs = kit.parse_knobs(knob_args, json.loads(previous) if previous else None)
    previous_sizing = secrets.get(hctx.user, kit.name, "sizing.json")
    sizing = kits.parse_sizing(kit, {k: v for k, v in sizing_args.items() if v},
                               json.loads(previous_sizing) if previous_sizing else None)
    plan = kits.apply_sizing(kit, kit.expand(knobs), sizing)
    trace.logger("cli").info("kit plan", kit=kit.name, knobs=knobs, sizing=sizing, machines=[m.name for m in plan])
    for m in plan:
        lifecycle.check_resize(hctx, m)  # before anything changes
    if sizing:  # saved up front: hooks such as the forge's pool configuration read it
        secrets.put(hctx.user, kit.name, "sizing.json", json.dumps(sizing))
    hooks = kits.hooks(kit.name)
    log = _log(ctx)
    started = time.monotonic()
    with _session() as conn:
        lifecycle.require_session_ready(conn)
        for m in plan:
            with trace.span("hooks.credentials", kit=kit.name, machine=m.name):
                kit_creds = hooks.credentials(hctx, m, plan)
            lifecycle.up(conn, hctx, m, log, kit_creds, **kits.installer_hooks(hooks, hctx, m, plan))
            with trace.span("hooks.after_ready", kit=kit.name, machine=m.name):
                hooks.after_ready(hctx, m, plan, knobs, log)
        with trace.span("hooks.finish", kit=kit.name):
            hooks.finish(hctx, plan, knobs, log)
        if kit.knobs:
            secrets.put(hctx.user, kit.name, "knobs.json", json.dumps(knobs))
        infos = lifecycle.kit_machines(conn, hctx.cfg.install_id, kit.name)
    rows = [_machine_row(i, hctx) for i in infos]
    if _json(ctx):
        _emit(ctx, {"kit": kit.name, "knobs": knobs, "sizing": sizing, "machines": rows})
    else:
        click.secho(f"✓ {kit.name} is ready ({time.monotonic() - started:.0f}s)", fg="green")
        _print_rows(rows)
        steps = hooks.next_steps(hctx, plan)
        if steps:
            click.echo()
            for step in steps:
                click.echo(step)
        else:
            click.echo(f"\nNext: hitkit creds {kit.name}    hitkit ssh {plan[0].name}")


@cli.command()
@click.argument("kit_name")
@click.pass_context
def down(ctx, kit_name) -> None:
    """Shut down a kit's machines, keeping everything."""
    from hitkit import kits
    from hitkit.machine import lifecycle

    kits.load(kit_name)
    hctx = _host_context()
    hooks = kits.hooks(kit_name)
    if hasattr(hooks, "before_down"):
        hooks.before_down(hctx)
    stopped = []
    with _session() as conn:
        for info in lifecycle.kit_machines(conn, hctx.cfg.install_id, kit_name):
            if lifecycle.stop(conn, hctx, info.name):
                stopped.append(info.name)
    if _json(ctx):
        _emit(ctx, {"stopped": stopped})
    else:
        click.echo(f"Stopped: {', '.join(stopped) or 'nothing'}")


@cli.command()
@click.argument("kit_name")
@click.option("--delete-data", is_flag=True,
              help="Also delete the data disks and the kit's secrets. Without it they are kept for the next `up`.")
@click.option("--keep-data", is_flag=True, hidden=True)  # the default since data disks are never deleted implicitly
@click.option("--force", is_flag=True, help="Also remove machines with indeterminate ownership.")
@click.option("--yes", is_flag=True, help="Confirm removal (required).")
@click.pass_context
def destroy(ctx, kit_name, delete_data, keep_data, force, yes) -> None:
    """Remove a kit's machines. Data disks and secrets are kept unless --delete-data."""
    from hitkit import kits, secrets
    from hitkit.errors import HitkitError
    from hitkit.machine import lifecycle

    if not yes:
        raise HitkitError(f"refusing to destroy {kit_name!r} without --yes")
    if delete_data and keep_data:
        raise HitkitError("--delete-data and --keep-data contradict each other")
    kit = kits.load(kit_name)
    hctx = _host_context()
    hooks = kits.hooks(kit.name)
    if delete_data:
        from hitkit import backup

        _log(ctx)(backup.age_notice(hctx.user, kit.name))
    removed = []
    with _session() as conn:
        names = {i.name for i in lifecycle.kit_machines(conn, hctx.cfg.install_id, kit.name)}
        names |= {m.name for m in kit.machines}
        for name in sorted(names):
            spec = kit.spec_for(name)
            if lifecycle.destroy(conn, hctx, name, force=force, delete_data=delete_data,
                                 stateful_os=bool(spec and spec.stateful_os)):
                removed.append(name)
    if delete_data:
        if hasattr(hooks, "destroyed"):
            hooks.destroyed(hctx)
        secrets.remove_kit(hctx.user, kit.name)
    if _json(ctx):
        _emit(ctx, {"removed": removed, "kept_data": not delete_data})
    else:
        click.echo(f"Removed: {', '.join(removed) or 'nothing'}" + (
            "" if delete_data else " (data disks and secrets kept; the next `up` reuses them)"))


@cli.command()
@click.argument("kit_name")
@click.pass_context
def creds(ctx, kit_name) -> None:
    """URLs and human-facing logins for a kit."""
    from hitkit import kits

    kits.load(kit_name)
    data = kits.hooks(kit_name).creds(_host_context())
    if not data:
        data = {"ssh": f"hitkit ssh {kits.load(kit_name).machines[0].name}"}
    if _json(ctx):
        _emit(ctx, data)
    else:
        width = max(len(k) for k in data)
        for k, v in data.items():
            click.echo(f"{k:<{width}}  {v}")


@cli.command("browser-cert")
@click.argument("kit_name")
@click.option("--remove", is_flag=True, help="Remove the certificate from those databases instead.")
@click.option("--db", "dbs", multiple=True, type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="An NSS database directory, repeatable (default: every Firefox profile and ~/.pki/nssdb).")
@click.pass_context
def browser_cert(ctx, kit_name, remove, dbs) -> None:
    """Import a kit's browser login certificate into Firefox and Chromium. No root needed.

    Firefox keeps a certificate database per profile; Chromium shares ~/.pki/nssdb. A
    browser picks the certificate up after a restart. Re-running is harmless, and replaces
    a certificate the kit has since regenerated.
    """
    from hitkit import kits
    from hitkit.errors import HitkitError
    from hitkit.host import nssdb

    kits.load(kit_name)
    hctx = _host_context()
    cert = kits.hooks(kit_name).browser_certificate(hctx)
    if cert is None:
        raise HitkitError(f"kit {kit_name!r} has no browser login certificate")
    targets = list(dbs) or nssdb.databases(hctx.user.home)
    if not targets:
        raise HitkitError("found no Firefox profile and no ~/.pki/nssdb",
                          hint="start each browser once (it creates its certificate database), or pass --db")
    results = []
    for db in targets:
        try:
            if remove:
                result = "removed" if nssdb.remove(db, cert["nickname"]) else "absent"
            else:
                result = nssdb.import_bundle(db, cert["bundle"], cert["password"], cert["nickname"],
                                             cert["certificate"])
            results.append({"db": str(db), "result": result})
        except HitkitError as e:
            results.append({"db": str(db), "result": "failed", "error": str(e), "hint": e.hint})
    if _json(ctx):
        _emit(ctx, results)
    else:
        for r in results:
            click.echo(f"{r['result']:<9} {r['db']}" + (f"  ({r['error']}; {r['hint']})" if "error" in r else ""))
        if any(r["result"] in ("imported", "replaced") for r in results):
            click.echo(f"Restart the browser to pick it up; it appears as {cert['nickname']!r}.")
    failed = [r["db"] for r in results if r["result"] == "failed"]
    if failed:
        raise HitkitError(f"could not update {', '.join(failed)}")


@cli.group()
def token() -> None:
    """Scoped service tokens for people and agents."""


def _token_hooks(kit_name: str):
    from hitkit import kits
    from hitkit.errors import HitkitError

    kits.load(kit_name)
    hooks = kits.hooks(kit_name)
    if not hasattr(hooks, "token_create"):
        raise HitkitError(f"kit {kit_name!r} has no service tokens")
    return hooks


@token.command("create")
@click.argument("kit_name")
@click.option("--name", required=True, help="Token name (unique per user).")
@click.option("--scope", "scopes", multiple=True,
              help="forge: a scope, repeatable (e.g. write:repository; default repo/issue/package access). "
                   "netbird: the service user's role (default admin).")
@click.pass_context
def token_create(ctx, kit_name, name, scopes) -> None:
    """Mint a scoped token; it is shown once."""
    data = _token_hooks(kit_name).token_create(_host_context(), name, list(scopes) or None)
    if _json(ctx):
        _emit(ctx, data)
    else:
        click.echo(data["token"])
        tail = f", expires {data['expires']}" if data.get("expires") else ""
        click.echo(f"(user {data['user']}, scopes: {', '.join(data['scopes'])}{tail})", err=True)


@token.command("list")
@click.argument("kit_name")
@click.pass_context
def token_list(ctx, kit_name) -> None:
    data = _token_hooks(kit_name).token_list(_host_context())
    if _json(ctx):
        _emit(ctx, data)
    else:
        for t in data:
            tail = f"  expires {t['expires']}" if t.get("expires") else ""
            click.echo(f"{t['name']:<24} …{t['last_eight'] or ''}  {', '.join(t['scopes'])}{tail}")


@token.command("revoke")
@click.argument("kit_name")
@click.argument("name")
@click.pass_context
def token_revoke(ctx, kit_name, name) -> None:
    _token_hooks(kit_name).token_revoke(_host_context(), name)
    if _json(ctx):
        _emit(ctx, {"revoked": name})
    else:
        click.echo(f"Revoked {name}")


def _existing_plan(conn, hctx, kit):
    """The kit's machines as they exist now (scaled machines included), as specs."""
    from hitkit.machine import lifecycle

    infos = lifecycle.kit_machines(conn, hctx.cfg.install_id, kit.name)
    knobs = kit.default_knobs()
    for t in kit.templates:
        if t.count_knob:
            knobs[t.count_knob] = sum(1 for i in infos if i.name.startswith(f"{t.prefix}-")) or knobs[t.count_knob]
    existing = {i.name for i in infos}
    from hitkit import kits, secrets

    sizing = secrets.get(hctx.user, kit.name, "sizing.json")
    plan = kits.apply_sizing(kit, kit.expand(knobs), json.loads(sizing) if sizing else None)
    return [m for m in plan if m.name in existing], knobs


@cli.command()
@click.argument("kit_name")
@click.pass_context
def upgrade(ctx, kit_name) -> None:
    """Move a kit's machines onto their roles' current images, keeping their data.

    Each machine is stopped, gets a fresh OS overlay on the new image, and must
    report ready; otherwise it is rolled back to its previous overlay.
    """
    from hitkit import kits, trace
    from hitkit.machine import lifecycle

    kit = kits.load(kit_name)
    hooks = kits.hooks(kit.name)
    hctx = _host_context()
    log = _log(ctx)
    upgraded = []
    with _session() as conn:
        plan, knobs = _existing_plan(conn, hctx, kit)
        for m in plan:
            with trace.span("hooks.credentials", kit=kit.name, machine=m.name):
                kit_creds = hooks.credentials(hctx, m, tuple(plan))
            if lifecycle.upgrade(conn, hctx, m, log, kit_creds):
                upgraded.append(m.name)
            with trace.span("hooks.after_ready", kit=kit.name, machine=m.name):
                hooks.after_ready(hctx, m, tuple(plan), knobs, log)
        if plan:
            with trace.span("hooks.finish", kit=kit.name):
                hooks.finish(hctx, tuple(plan), knobs, log)
    if _json(ctx):
        _emit(ctx, {"upgraded": upgraded})
    else:
        click.echo(f"Upgraded: {', '.join(upgraded) or 'nothing (all current)'}")


def _forge_hooks():
    from hitkit import kits

    return kits.hooks("forge")


@cli.command()
@click.argument("repo", metavar="OWNER/REPO")
@click.argument("account")
@click.option("--permission", type=click.Choice(["read", "write", "admin"]), default="write", show_default=True)
@click.pass_context
def grant(ctx, repo, account, permission) -> None:
    """Give a forge account such as hitkit-dev or renovate-bot access to one repository."""
    _forge_hooks().grant(_host_context(), repo, account, permission)
    if _json(ctx):
        _emit(ctx, {"repo": repo, "account": account, "permission": permission})
    else:
        click.echo(f"{account} now has {permission} access to {repo}")


@cli.group()
def mirror() -> None:
    """Push mirrors from the local forge to GitHub, GitLab, or any git remote."""


@mirror.command("add")
@click.argument("repo", metavar="OWNER/REPO")
@click.argument("remote", metavar="REMOTE_URL")
@click.option("--username", help="Remote username (guessed for github.com and GitLab).")
@click.option("--token-env", default="HITKIT_MIRROR_TOKEN", show_default=True,
              help="Environment variable holding the remote token; read from stdin if unset.")
@click.option("--interval", default="8h0m0s", show_default=True, help="Periodic sync interval.")
@click.option("--no-sync-on-commit", is_flag=True, help="Only sync on the interval.")
@click.pass_context
def mirror_add(ctx, repo, remote, username, token_env, interval, no_sync_on_commit) -> None:
    """Mirror REPO to REMOTE_URL. The token is never taken from the command line."""
    from hitkit.errors import HitkitError

    token = os.environ.get(token_env)
    if token is None:
        if sys.stdin.isatty():
            raise HitkitError(f"no token: set {token_env} or pipe it on stdin")
        token = sys.stdin.read().strip()
    if not token:
        raise HitkitError("empty remote token")
    from hitkit import trace

    trace.secret(token)
    result = _forge_hooks().mirror_add(_host_context(), repo, remote, username, token, interval, not no_sync_on_commit)
    if _json(ctx):
        _emit(ctx, {"name": result.get("remote_name"), "remote": remote})
    else:
        click.echo(f"Mirroring {repo} -> {remote} ({result.get('remote_name')})")


@mirror.command("list")
@click.argument("repo", metavar="OWNER/REPO")
@click.pass_context
def mirror_list(ctx, repo) -> None:
    data = _forge_hooks().mirror_list(_host_context(), repo)
    if _json(ctx):
        _emit(ctx, data)
        return
    if not data:
        click.echo("No push mirrors.")
    for m in data:
        err = f"  error: {m['last_error']}" if m["last_error"] else ""
        click.echo(f"{m['name']:<20} {m['remote']}  last sync {m['last_update'] or 'never'}{err}")


@mirror.command("sync")
@click.argument("repo", metavar="OWNER/REPO")
@click.pass_context
def mirror_sync(ctx, repo) -> None:
    """Push to every mirror of REPO now."""
    _forge_hooks().mirror_sync(_host_context(), repo)
    _emit(ctx, {"synced": repo}) if _json(ctx) else click.echo(f"Sync started for {repo}")


@mirror.command("remove")
@click.argument("repo", metavar="OWNER/REPO")
@click.argument("name")
@click.pass_context
def mirror_remove(ctx, repo, name) -> None:
    _forge_hooks().mirror_remove(_host_context(), repo, name)
    _emit(ctx, {"removed": name}) if _json(ctx) else click.echo(f"Removed mirror {name}")


@cli.command("backup")
@click.argument("kit_name", required=False)
@click.option("--dest", help="Backup root directory (default ~/.local/share/hitkit/backups; prefer another disk). Remembered.")
@click.option("--keep", type=click.IntRange(1, 1000), help=f"How many backups to keep (default 7). Remembered.")
@click.option("--online", "method", flag_value="online", help="Copy /srv/data live (the kit's own default per machine otherwise).")
@click.option("--offline", "method", flag_value="offline", help="Stop each machine and copy its data disk exactly.")
@click.option("--schedule", type=click.Choice(["daily", "off"]), help="Install or remove a daily user timer; no backup now.")
@click.option("--list", "list_only", is_flag=True, help="List backups (of KIT, or of every kit).")
@click.pass_context
def backup_cmd(ctx, kit_name, dest, keep, method, schedule, list_only) -> None:
    """Back up a kit's data (/srv/data of each machine) and secrets.

    The forge is copied live with Forgejo stopped for seconds; other kits' machines are
    stopped for an exact copy of their data disk. Restore with `hitkit restore`.
    """
    from hitkit import backup, kits
    from hitkit.errors import HitkitError

    user = current_user()
    if list_only:
        names = [kit_name] if kit_name else [k.name for k in kits.all_kits()]
        rows = []
        for name in names:
            settings = backup.Settings.load(user, name)
            rows += [{"kit": b.kit, "created": b.created, "path": str(b.path), "size_bytes": b.size_bytes,
                      "machines": sorted(b.machines), "schedule": settings.schedule}
                     for b in backup.list_backups(user, name, settings.dest)]
        if _json(ctx):
            _emit(ctx, rows)
        elif not rows:
            click.echo("No backups.")
        for r in rows if not _json(ctx) else []:
            click.echo(f"{r['kit']:<10} {r['created']}  {_gib(r['size_bytes']):>10}  {r['path']}")
        return
    if not kit_name:
        raise HitkitError("which kit? hitkit backup <kit>", hint="hitkit backup --list")
    kit = kits.load(kit_name)
    settings = backup.Settings.load(user, kit.name)
    if dest is not None:
        settings.dest = str(Path(dest).expanduser().resolve())
    if keep is not None:
        settings.keep = keep
    if schedule:
        backup.set_schedule(user, kit.name, schedule)
        settings.schedule = schedule
        settings.save(user, kit.name)
        _emit(ctx, {"kit": kit.name, "schedule": schedule}) if _json(ctx) else click.echo(
            f"Backups of {kit.name}: {schedule}" + (f" (timer hitkit-backup@{kit.name}.timer)" if schedule == "daily" else ""))
        return
    hctx = _host_context()
    with _session() as conn:
        path = backup.create(conn, hctx, kit, kits.hooks(kit.name), settings.dest, settings.keep, _log(ctx), method)
    settings.save(hctx.user, kit.name)
    info = next(b for b in backup.list_backups(hctx.user, kit.name, settings.dest) if b.path == path)
    if _json(ctx):
        _emit(ctx, {"kit": kit.name, "path": str(path), "size_bytes": info.size_bytes, "machines": sorted(info.machines)})
    else:
        click.secho(f"✓ backed up {kit.name} to {path} ({_gib(info.size_bytes)})", fg="green")


@cli.command("restore")
@click.argument("kit_name")
@click.argument("backup_path", required=False, metavar="[BACKUP_DIR]")
@click.option("--yes", is_flag=True, help="Confirm (required).")
@click.pass_context
def restore_cmd(ctx, kit_name, backup_path, yes) -> None:
    """Restore a kit from a backup (default: the newest), then re-wire it like `up`.

    Nothing current is deleted: the machine's current /srv/data contents move to
    /srv/data/.hitkit-pre-restore-<time>, or its current data disk to vms/.kept, and the
    current secrets to secrets/.pre-restore/.
    """
    from hitkit import backup, kits
    from hitkit.errors import HitkitError

    if not yes:
        raise HitkitError(f"refusing to restore {kit_name!r} without --yes")
    kit = kits.load(kit_name)
    hctx = _host_context()
    if backup_path:
        path = Path(backup_path).expanduser()
    else:
        settings = backup.Settings.load(hctx.user, kit.name)
        found = backup.list_backups(hctx.user, kit.name, settings.dest)
        if not found:
            raise HitkitError(f"no backups of {kit.name}", hint=f"hitkit backup {kit.name}")
        path = found[-1].path
    with _session() as conn:
        moved = backup.restore(conn, hctx, kit, kits.hooks(kit.name), path, _log(ctx))
    _log(ctx)(f"restored {kit.name} from {path}; re-wiring it")
    _bring_up(ctx, kit.name, [], {})
    if not _json(ctx):
        click.echo("Previous state kept at:")
        for what, where in moved.items():
            if where and where != "None":
                click.echo(f"  {what:<10} {where}")


@cli.group()
def pool() -> None:
    """The forge's ephemeral runner pool (one fresh VM per CI job)."""


@pool.command("run")
def pool_run() -> None:
    """Run the pool's reconcile loop in the foreground (what hitkit-pool.service runs)."""
    from hitkit import forgepool, trace

    hctx = _host_context()
    pool_log = trace.logger("pool")

    def log(msg: str) -> None:
        pool_log.info(msg)
        click.echo(f"{time.strftime('%H:%M:%S')} {msg}", err=True)

    forgepool.run_forever(hctx, lambda: _forge_hooks().admin_api(hctx), log)


@pool.command("status")
@click.pass_context
def pool_status(ctx) -> None:
    from hitkit import forgepool
    from hitkit.machine import lifecycle

    hctx = _host_context()
    cfg = forgepool.load_config(hctx.user) or {}
    with _session() as conn:
        vms = [i.name for i in lifecycle.kit_machines(conn, hctx.cfg.install_id, "forge")
               if i.name.startswith(forgepool.PREFIX)]
    flavors = forgepool.flavors(cfg) if cfg.get("max") else []
    data = {"enabled": bool(cfg.get("max")), "max": cfg.get("max", 0),
            "labels": [label for f in flavors for label in f["labels"]],
            "flavors": [{"labels": f["labels"], "role": f["spec"]["role"], "template": f.get("template")}
                        for f in flavors],
            "service_active": forgepool.service_active(), "job_vms": vms}
    if _json(ctx):
        _emit(ctx, data)
    else:
        for k, v in data.items():
            click.echo(f"{k:<15} {v}")


@cli.command()
@click.argument("kit_name", required=False)
@click.pass_context
def status(ctx, kit_name) -> None:
    """Machines, state, addresses, and images."""
    from hitkit.machine import lifecycle

    hctx = _host_context()
    with _session() as conn:
        infos = lifecycle.list_machines(conn, hctx.cfg.install_id)
    rows = [_machine_row(i, hctx) for i in infos if kit_name is None or (i.meta and i.meta.kit == kit_name)]
    if _json(ctx):
        _emit(ctx, rows)
        return
    _print_rows(rows)
    kept = [k for k in lifecycle.kept_disks(hctx) if kit_name is None or k.machine in {r["name"] for r in rows}]
    if kept:
        click.echo("\nKept disks, reused by the next create (see `hitkit gc`):")
        for k in kept:
            click.echo(f"  {k.machine:<12} {k.kind:<5} {_gib(k.size_bytes):>10}  {k.modified[:10]}  {k.path}")


@cli.command(context_settings={"ignore_unknown_options": True})
@click.option("--user", "-u", "login", default="root", show_default=True, help="Remote user.")
@click.argument("machine")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def ssh(login, machine, command) -> None:
    """SSH into a machine using its pinned host key."""
    from hitkit import kits, trace
    from hitkit.errors import HitkitError
    from hitkit.machine import images, remote

    spec = next((s for k in kits.all_kits() if (s := k.spec_for(machine))), None)
    if spec is not None and images.is_installer_role(spec.role):
        raise HitkitError(f"{machine} has no shell: it runs its vendor's appliance OS",
                          hint=f"hitkit creds {spec.kit} shows how to manage it")
    argv =remote.ssh_args(_host_context(), machine, login, interactive=True) + list(command)
    trace.logger("cli").info("exec ssh", argv=argv)
    os.execvp("ssh", argv)


@cli.command()
@click.argument("machine")
@click.option("-f", "--follow", is_flag=True, help="Keep printing new output.")
def logs(machine, follow) -> None:
    """Show a machine's serial console log."""
    from hitkit.errors import HitkitError

    log_path = _host_context().paths(machine).console_log
    if not log_path.exists():
        raise HitkitError(f"no console log for {machine!r}", hint="hitkit status")
    with log_path.open(errors="replace") as fh:
        sys.stdout.write(fh.read())
        while follow:
            chunk = fh.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            else:
                time.sleep(0.5)


@cli.command("trace")
@click.argument("ref", metavar="[TRACE_ID|last]", required=False)
@click.option("-n", "--limit", default=20, show_default=True, help="How many recent invocations to list.")
@click.option("--failed", "only_failed", is_flag=True, help="Only failed invocations.")
@click.pass_context
def trace_cmd(ctx, ref, limit, only_failed) -> None:
    """Recorded traces: list recent invocations, or show every record of one.

    Errors print their trace id; `hitkit trace --failed last` shows the latest failure.
    Records live in ~/.local/state/hitkit/logs (JSON lines, kept 14 days).
    """
    from hitkit import trace

    user = current_user()
    if ref:
        records = trace.select(user, ref, only_failed)
        if _json(ctx):
            _emit(ctx, records)
        else:
            click.echo(f"trace {records[0]['trace']}: hitkit {records[0].get('cmd', '')}")
            for r in records:
                click.echo(trace.format_human(r))
        return
    rows = [t for t in trace.summarize(trace.read(user)) if trace.failed(t) or not only_failed][-limit:]
    if _json(ctx):
        _emit(ctx, rows)
        return
    if not rows:
        click.echo("No traces recorded.")
    for t in rows:
        outcome = "failed" if trace.failed(t) else "ok" if t["exit_code"] == 0 else "-"
        took = f"{t['duration_ms'] / 1000:.1f}s" if t["duration_ms"] is not None else "-"
        click.echo(f"{trace.local_time(t['start'], '%Y-%m-%d %H:%M:%S')}  {t['trace']}  {outcome:<6} {took:>7}  {t['cmd']}")


@cli.group("skills", invoke_without_command=True)
@click.pass_context
def skills_cmd(ctx) -> None:
    """Agent skills for using hitkit (Agent Skills format, any harness).

    Without a subcommand, lists the bundled skills."""
    if ctx.invoked_subcommand is not None:
        return
    from hitkit import agentskills

    skills = agentskills.available()
    if _json(ctx):
        _emit(ctx, [{"name": s.name, "path": str(s.path), "description": s.description} for s in skills])
        return
    for s in skills:
        click.echo(f"{s.name:<20} {s.path}")


@skills_cmd.command("install")
@click.argument("dest", required=False, type=click.Path(file_okay=False, path_type=Path))
@click.option("--yes", "-y", is_flag=True, help="Create ./.agents without asking when installing to the default location.")
@click.pass_context
def skills_install(ctx, dest, yes) -> None:
    """Copy the bundled skills into DEST, a harness's skills directory (e.g. ~/.claude/skills).

    Without DEST, installs into .agents/skills of the current directory, asking first
    to create .agents if it does not exist. Re-running refreshes hitkit's own copies."""
    from hitkit import agentskills

    if dest is None:
        agents = Path.cwd() / ".agents"
        if not agents.is_dir() and not yes:
            if not click.confirm(f"{agents} does not exist. Create it?", default=True, err=True):
                raise HitkitError("not installed", hint="pass a directory: hitkit skills install DIR")
        dest = agents / "skills"
    installed = agentskills.install(dest.expanduser())
    _emit(ctx, [str(p) for p in installed]) if _json(ctx) else click.echo(
        "\n".join(f"installed {p}" for p in installed))


@skills_cmd.command("print")
@click.argument("name")
@click.pass_context
def skills_print(ctx, name) -> None:
    """Print the SKILL.md of the bundled skill NAME."""
    from hitkit import agentskills

    text = (agentskills.find(name).path / "SKILL.md").read_text()
    if _json(ctx):
        _emit(ctx, {"name": name, "content": text})
    else:
        click.echo(text, nl=not text.endswith("\n"))


@cli.group()
def image() -> None:
    """Build and manage cached machine images."""


@image.command("build")
@click.argument("role", default="base")
@click.pass_context
def image_build(ctx, role) -> None:
    """Build the image for ROLE (default: base), even if cached."""
    from hitkit.machine import images

    img = images.build(current_user(), role, _log(ctx))
    if _json(ctx):
        _emit(ctx, {"id": img.id, "path": str(img.path), "versions": img.versions})
    else:
        click.secho(f"✓ built {img.id}", fg="green")


@image.command("ls")
@click.pass_context
def image_ls(ctx) -> None:
    """List cached images and the versions they contain."""
    from hitkit.machine import images

    user = current_user()
    data = []
    for img in images.list_images(user):
        current = images.find(user, img.role)
        data.append({"id": img.id, "role": img.role, "current": bool(current and current.id == img.id),
                     "path": str(img.path), "versions": img.versions})
    if _json(ctx):
        _emit(ctx, data)
        return
    if not data:
        click.echo("No cached images.")
    for d in data:
        mark = "*" if d["current"] else " "
        vers = ", ".join(f"{k} {v}" for k, v in d["versions"].items())
        click.echo(f"{mark} {d['id']:<20} {vers}")


def _gc(ctx, yes: bool, include_dirs: bool, delete_data: bool = False) -> None:
    from hitkit.machine import images, lifecycle

    hctx = _host_context()
    with _session() as conn:
        dirs = lifecycle.orphan_dirs(conn, hctx) if include_dirs else []
    unused = lifecycle.unreferenced_images(hctx)
    kept = lifecycle.kept_disks(hctx) if include_dirs else []
    preserved = []
    if yes:
        for d in dirs:
            preserved += lifecycle.remove_orphan(hctx, d, stateful_os=_stateful(None, d.name))
        for img in unused:
            images.remove(img)
        if delete_data:
            for k in kept:
                lifecycle.delete_kept_disk(k.path)
    data = {"orphan_dirs": [str(d) for d in dirs], "images": [i.id for i in unused],
            "kept_disks": [{"machine": k.machine, "kind": k.kind, "path": str(k.path), "size_bytes": k.size_bytes,
                            "modified": k.modified, "reused_by_up": k.reused_by_up} for k in kept],
            "preserved": [str(p) for p in preserved], "deleted_kept_disks": yes and delete_data,
            "removed": yes}
    if _json(ctx):
        _emit(ctx, data)
        return
    if not dirs and not unused and not kept:
        click.echo("Nothing to clean up.")
        return
    verb = "Removed" if yes else "Would remove (pass --yes)"
    for d in dirs:
        click.echo(f"{verb}: orphaned machine directory {d} (its data disk is kept)")
    for i in unused:
        click.echo(f"{verb}: unused image {i.id}")
    for p in preserved:
        click.echo(f"Kept data disk: {p}")
    for k in kept:
        if yes and delete_data:
            click.echo(f"Deleted kept data disk: {k.path}")
        else:
            note = "the newest; the next create reuses it" if k.reused_by_up else "older; kept for you to inspect or delete"
            click.echo(f"Kept {k.kind} disk: {k.path} ({_gib(k.size_bytes)}, {k.modified[:10]}; {note})")
    if kept and not (yes and delete_data):
        click.echo("Kept disks are only deleted by `hitkit gc --delete-data --yes`.")


def _gib(n: int) -> str:
    return f"{n / 2**30:.1f} GiB"


@image.command("prune")
@click.option("--yes", is_flag=True, help="Actually remove.")
@click.pass_context
def image_prune(ctx, yes) -> None:
    """Remove images that are neither current nor backing a machine."""
    _gc(ctx, yes, include_dirs=False)


@cli.command()
@click.option("--yes", is_flag=True, help="Actually remove.")
@click.option("--delete-data", is_flag=True, help="Also delete kept data disks (vms/.kept). Irreversible.")
@click.pass_context
def gc(ctx, yes, delete_data) -> None:
    """Remove orphaned machine directories (keeping data disks) and unreferenced images."""
    _gc(ctx, yes, include_dirs=True, delete_data=delete_data)


def main() -> None:
    cli()
