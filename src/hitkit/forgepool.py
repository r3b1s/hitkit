"""Ephemeral runner pool: one fresh VM per CI job (roadmap; design §5 accepted risk).

A host-side reconcile loop (`hitkit pool run`, installed as the user service
`hitkit-pool.service`) watches Forgejo's job queue. For every waiting job whose
labels the pool serves, it registers an *ephemeral* runner (Forgejo deletes it
after one job) and boots a `ci-N` VM holding only that runner's token and the
job's handle. The VM runs exactly that job and powers itself off; the loop then
destroys it. Nothing carries over between jobs.

State lives in libvirt (the ci-N domains) plus one small pool.json per VM, so a
crashed or restarted loop picks up where it left off. Job VMs boot with their
data disk overlaid on a warm template that already holds the job images.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from hitkit import secrets, trace
from hitkit.errors import HitkitError
from hitkit.forgejo import Forgejo, ForgejoError
from hitkit.machine import lifecycle
from hitkit.machine.spec import MachineSpec
from hitkit.paths import UserContext, write_private

KIT = "forge"
PREFIX = "ci-"
MAX_SLOTS = 99  # ci-1 .. ci-99; ci-100 is reserved for template builds
TEMPLATE_SLOT = "ci-100"
JOB_TIMEOUT = 4 * 3600 + 600  # runner timeout (3h) plus boot/cleanup slack
UNIT_NAME = "hitkit-pool.service"


# --- configuration (written by `hitkit up forge --ephemeral N`) ------------


def load_config(user: UserContext) -> dict | None:
    raw = secrets.get(user, KIT, "pool.json")
    return json.loads(raw) if raw else None


def save_config(user: UserContext, cfg: dict) -> None:
    secrets.put(user, KIT, "pool.json", json.dumps(cfg, indent=2))


def flavors(cfg: dict) -> list[dict]:
    """The kinds of job VM the pool starts: the labels each serves, its machine spec, and
    its warm data template (None: a blank data disk). pool.json from before flavors
    describes exactly one."""
    if "flavors" in cfg:
        return cfg["flavors"]
    return [{"labels": cfg["labels"], "spec": cfg["spec"], "template": cfg.get("template")}]


def flavor_for(job: dict, candidates: list[dict]) -> dict | None:
    """The first flavor whose labels cover everything the job's `runs-on` asks for."""
    wanted = set(job.get("runs_on") or [])
    return next((f for f in candidates if wanted <= set(f["labels"])), None)


def labels_for(persistent_runners: int) -> list[str]:
    """`runs-on: ephemeral` always gets a fresh VM; with no persistent runners
    the pool also serves the ordinary labels."""
    return ["ephemeral"] + (["ubuntu-latest", "ubuntu-24.04"] if persistent_runners == 0 else [])


def job_spec(base: MachineSpec, name: str) -> MachineSpec:
    return replace(base, name=name)


# --- warm template ----------------------------------------------------------


def template_path(user: UserContext, prepull: list[str], runner_image: str) -> Path:
    """Keyed on the images to pull and the runner image (its storage layout must match)."""
    digest = hashlib.sha256(("\n".join(sorted(prepull)) + runner_image).encode()).hexdigest()[:12]
    return user.cache_dir / "templates" / f"runner-data-{digest}.qcow2"


def ensure_template(conn, hctx, base: MachineSpec, prepull: list[str], log: Callable[[str], None]) -> Path:
    """Build (once) a data disk with every job image pulled, used read-only as a backing file."""
    from hitkit.machine import images

    path = template_path(hctx.user, prepull, images.image_id(base.role, hctx.user))
    if path.exists():
        return path
    log("building the warm job-VM template (pulls job images once; takes a few minutes)")
    lifecycle.destroy(conn, hctx, TEMPLATE_SLOT, force=True, delete_data=True)
    spec = job_spec(base, TEMPLATE_SLOT)
    dom = lifecycle.create(conn, hctx, spec, log, {"hitkit.runner.prepull": "\n".join(prepull).encode()})
    console = hctx.paths(TEMPLATE_SLOT).console_log
    deadline = time.monotonic() + 3600
    while dom.isActive():
        if time.monotonic() > deadline:
            lifecycle.destroy(conn, hctx, TEMPLATE_SLOT, force=True, delete_data=True)
            raise HitkitError("template build timed out", hint=f"see {console}")
        time.sleep(5)
    log_text = console.read_text(errors="replace")
    pulled = log_text.count("prepull: ")
    if "HITKIT-PREPULL-DONE" not in log_text or pulled == 0:
        log_copy = hctx.user.cache_dir / "templates" / "last-failed-build.log"
        log_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(console, log_copy)
        lifecycle.destroy(conn, hctx, TEMPLATE_SLOT, force=True, delete_data=True)
        raise HitkitError("template build failed while pulling job images", hint=f"see {log_copy}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    hctx.paths(TEMPLATE_SLOT).data_disk.replace(tmp)
    tmp.chmod(0o444)  # a backing file must never change
    tmp.replace(path)
    lifecycle.destroy(conn, hctx, TEMPLATE_SLOT, force=True, delete_data=True)
    return path


# --- per-VM state -----------------------------------------------------------


def _state_path(hctx, name: str) -> Path:
    return hctx.paths(name).root / "pool.json"


def _read_state(hctx, name: str) -> dict | None:
    try:
        return json.loads(_state_path(hctx, name).read_text())
    except (FileNotFoundError, ValueError):
        return None


# --- the loop -----------------------------------------------------------------


class Pool:
    def __init__(self, hctx, admin: Forgejo, log: Callable[[str], None]):
        self.hctx, self.admin, self.log = hctx, admin, log

    def tick(self, conn, cfg: dict) -> dict:
        """One reconcile pass. Returns a summary (for logs and `hitkit pool status`)."""
        hctx = self.hctx
        machines = [i for i in lifecycle.kit_machines(conn, hctx.cfg.install_id, KIT)
                    if i.name.startswith(PREFIX) and i.name != TEMPLATE_SLOT]
        alive: dict[str, dict] = {}
        for info in machines:
            state = _read_state(hctx, info.name)
            if info.state != "running" or state is None:
                self._reap(conn, info.name, state, "finished" if state else "no pool state")
            elif time.time() - state["started"] > JOB_TIMEOUT:
                self._reap(conn, info.name, state, "timed out")
            else:
                alive[info.name] = state
        self._sweep_runners({s["runner_id"] for s in alive.values()})

        started = []
        capacity = cfg["max"] - len(alive)
        # A flavor with a warm template can only start once that template is built.
        ready = [f for f in flavors(cfg) if not f.get("template") or Path(f["template"]).exists()]
        if capacity > 0 and ready:
            busy = {s["handle"] for s in alive.values()}
            labels = sorted({label for f in ready for label in f["labels"]})
            jobs = self.admin.get(f"/admin/actions/runners/jobs?labels={','.join(labels)}") or []
            waiting = [j for j in jobs if j["status"] == "waiting" and j["handle"] not in busy]
            used = {i.name for i in machines}
            free = [f"{PREFIX}{k}" for k in range(1, MAX_SLOTS + 1) if f"{PREFIX}{k}" not in used]
            for job in waiting:
                if len(started) >= capacity:
                    break
                flavor = flavor_for(job, ready)
                if flavor is not None:
                    started.append(self._start(conn, flavor, free.pop(0), job))
        return {"running": sorted(alive), "started": started, "max": cfg["max"]}

    def _start(self, conn, flavor: dict, name: str, job: dict) -> str:
        reg = self.admin.post("/admin/actions/runners", {
            "name": name, "ephemeral": True, "description": f"hitkit job VM for job {job['id']}",
        })
        creds = {
            "hitkit.runner.token": reg["token"].encode(),
            "hitkit.runner.uuid": reg["uuid"].encode(),
            "hitkit.runner.handle": job["handle"].encode(),
        }
        if self.hctx.paths("cache").known_hosts.exists():
            creds["hitkit.runner.cache"] = b"https://cache.hitkit.internal"
        base = MachineSpec(**flavor["spec"])
        template = Path(flavor["template"]) if flavor.get("template") else None
        try:
            lifecycle.create(conn, self.hctx, job_spec(base, name), self.log, creds,
                             data_backing=template, disposable=True)
        except BaseException:
            self.admin.delete(f"/admin/actions/runners/{reg['id']}")
            raise
        state = {"handle": job["handle"], "job_id": job["id"], "runner_id": reg["id"], "started": time.time()}
        write_private(_state_path(self.hctx, name), json.dumps(state).encode(), self.hctx.user)
        self.log(f"job {job['id']} ({','.join(job['runs_on'])}) -> {name}")
        return name

    def _reap(self, conn, name: str, state: dict | None, why: str) -> None:
        lifecycle.destroy(conn, self.hctx, name, force=True, delete_data=True)
        if state:
            self.admin.delete(f"/admin/actions/runners/{state['runner_id']}")
        self.log(f"{name} {why}; removed")

    def _sweep_runners(self, keep: set[int]) -> None:
        """Ephemeral ci-* registrations without a VM (e.g. after a crash)."""
        for runner in self.admin.get("/admin/actions/runners") or []:
            if runner.get("ephemeral") and runner["name"].startswith(PREFIX) and runner["id"] not in keep:
                self.admin.delete(f"/admin/actions/runners/{runner['id']}")

    def drain(self, conn) -> list[str]:
        """Remove every job VM (pool disabled or kit going down)."""
        names = [i.name for i in lifecycle.kit_machines(conn, self.hctx.cfg.install_id, KIT) if i.name.startswith(PREFIX)]
        for name in names:
            self._reap(conn, name, _read_state(self.hctx, name), "drained")
        self._sweep_runners(set())
        return names


def run_forever(hctx, admin_factory: Callable[[], Forgejo], log: Callable[[str], None], interval: float = 5) -> None:
    from hitkit import virt

    lock_path = hctx.user.data_dir / "pool.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HitkitError("another hitkit pool is already running") from None
        log("pool started")
        while True:
            trace.new_trace()  # one trace per pass; quiet passes record nothing at the default level
            cfg = load_config(hctx.user)
            try:
                with virt.connect(virt.SESSION_URI) as conn:
                    pool = Pool(hctx, admin_factory(), log)
                    if cfg is None or cfg.get("max", 0) == 0:
                        pool.drain(conn)
                    else:
                        pool.tick(conn, cfg)
            except (HitkitError, ForgejoError) as e:
                trace.logger(__name__).warning("pool tick failed", error=str(e), exc_info=True)
                log(f"tick failed: {e}")  # Forgejo restarting, libvirt hiccup: retry next tick
            time.sleep(interval)


# --- user service -------------------------------------------------------------


def unit_path(user: UserContext) -> Path:
    return user.config_dir.parent / "systemd" / "user" / UNIT_NAME


def unit_text() -> str:
    hitkit = Path(sys.argv[0]).resolve() if Path(sys.argv[0]).name == "hitkit" else shutil.which("hitkit")
    return (
        "[Unit]\n"
        "Description=hitkit ephemeral CI runner pool (one VM per job)\n"
        "Wants=hitkit-libvirt.service\n"
        "After=hitkit-libvirt.service\n\n"
        "[Service]\n"
        f"ExecStart={hitkit} pool run\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemctl_user(*args: str) -> None:
    trace.run(["systemctl", "--user", *args], check=True, capture_output=True)


def install_service(user: UserContext) -> None:
    path = unit_path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text())
    _systemctl_user("daemon-reload")
    _systemctl_user("enable", UNIT_NAME)
    # Always restart: a long-running loop must not keep running code (or reading config)
    # older than the hitkit that just reconfigured it. VMs live in hitkit-libvirt.service,
    # so restarting the loop never touches them.
    _systemctl_user("restart", UNIT_NAME)


def remove_service(user: UserContext) -> None:
    path = unit_path(user)
    if path.exists():
        trace.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], capture_output=True)
        path.unlink()
        trace.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


def service_active() -> bool:
    return trace.run(["systemctl", "--user", "is-active", "--quiet", UNIT_NAME]).returncode == 0
