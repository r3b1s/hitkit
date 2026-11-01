"""The incus-cluster kit (docs/plans/incus-cluster.md).

`finish` runs against an in-memory model of Incus and Operations Center that enforces what
the spike found (docs/incus/operations-center.md), so a wrong order fails here as it would
for real. The REST clients run over TLS against a small fake server at the end.
"""

from __future__ import annotations

import ipaddress
import json
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from test_incus_os import _self_signed

from hitkit import incus, kits, secrets
from hitkit.addressing import parse_subnet
from hitkit.errors import HitkitError
from hitkit.host.ca import create_ca
from hitkit.machine import images, seed
from hitkit.machine.lifecycle import HostContext
from hitkit.operations_center import OperationsCenterClient

SUBNET = parse_subnet("10.77.0.0/24")
KIT = "incus-cluster"
FIRST, SECOND, THIRD = "incus-cluster-1", "incus-cluster-2", "incus-cluster-3"
IPS = {"10.77.0.3": FIRST, "10.77.0.4": SECOND, "10.77.0.5": THIRD}
OC_IP = "10.77.0.6"


class CutOff(Exception):
    """`up` died here: not a HitkitError, because nothing in a dead process handles it."""


def member(node: str) -> str:
    return f"{node}.hitkit.internal"


class World:
    """One install: the nodes, the VM on the first, and Operations Center inside it."""

    def __init__(self, nodes=(FIRST,)):
        self.self_signed = {n: _self_signed(f"root@{n}")[0] for n in (FIRST, SECOND, THIRD)}
        self.nodes = {n: {"pools": set(), "volumes": {}, "config": {"core.https_address": ":8443"},
                          "registered": False, "cert": self.self_signed[n]} for n in nodes}
        self.vm = None  # {"status", "devices", "installed"}
        self.cluster = None  # {"members", "certificate", "provisioned"}
        self.stalled_calls = 0  # OC's resync holds its database: this many calls fail with a 500
        self.servers: dict[str, dict] = {}
        self.oc_cert = _self_signed("root@operations-center")[0]
        self.ops = 0
        self.cut_at: int | None = None
        self.cut_after = False  # the change lands, and only then does `up` die: the reply never came
        self.reply_lost = False
        self.stall_after_join = 0
        self.stalled_certificate_writes = 0  # reads have recovered; the write still hits the resync
        self.log: list[str] = []
        self.uploaded = None

    def act(self, what: str) -> None:
        """Every change to the world goes through here, so `up` can be cut off at any of them."""
        self.ops += 1
        if self.cut_at == self.ops:
            if not self.cut_after:
                raise CutOff(f"cut off before: {what}")
            self.reply_lost = True
        self.log.append(what)

    def oc_running(self) -> bool:
        return bool(self.vm and self.vm["status"] == "Running" and BOOT not in self.vm["devices"])


BOOT = "boot-media"


class Replies:
    """Wraps a client so that a call whose change landed can still lose its reply."""

    def __init__(self, world: World, client):
        self._w, self._c = world, client

    def __getattr__(self, name):
        attr = getattr(self._c, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            result = attr(*args, **kwargs)
            if self._w.reply_lost:
                self._w.reply_lost = False
                raise CutOff(f"reply lost: {name}{args[:1]}")
            return result

        return call


class Node:
    """The parts of incus.IncusClient `finish` uses."""

    def __init__(self, world: World, name: str, pin: str):
        self.w, self.name, self.n = world, name, world.nodes[name]
        if incus.fingerprint(self.n["cert"]) != pin:
            raise incus.PinMismatch(f"{name} presented a certificate other than the pinned one")

    def wait(self, op, timeout=600):
        return op

    def _clustered(self) -> bool:
        return bool(self.w.cluster) and member(self.name) in self.w.cluster["members"]

    def get(self, path: str):
        if path == "/1.0":
            return {"auth": "trusted", "environment": {"server_clustered": self._clustered()}}
        if path == "/1.0/cluster/members":
            return [f"/1.0/cluster/members/{m}" for m in self.w.cluster["members"]]
        if path == "/1.0/storage-pools":
            return [f"/1.0/storage-pools/{p}" for p in sorted(self.n["pools"])]
        if path == "/1.0/storage-pools/bootstrap/volumes/custom":
            return [f"{path}/{v}" for v in sorted(self.n["volumes"].get("bootstrap", ()))]
        if path == "/os/1.0/system/provider":
            return {"config": {"name": "images"}, "state": {"registered": self.n["registered"]}}
        if path == "/os/1.0/system/security":
            return {"config": {"encryption_recovery_keys": [f"enc-{self.name}"]},
                    "state": {"pool_recovery_keys": {"local": f"pool-{self.name}"}}}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, body=None):
        if path == "/1.0/storage-pools":
            assert body["name"] == "bootstrap" and body["config"]["source"] == "local/bootstrap"
            self.w.act("create pool bootstrap")
            self.n["pools"].add("bootstrap")
            self.n["volumes"]["bootstrap"] = set()
        elif path == "/1.0/storage-pools/bootstrap/volumes/custom":
            self.w.act("create volume bootstrap/logs")
            self.n["volumes"]["bootstrap"].add(body["name"])
        elif path == "/1.0/instances":
            self.w.act("create VM")
            assert body["config"]["security.secureboot"] == "false" and "vtpm" in body["devices"]
            assert body["devices"]["eth0"]["nictype"] == "bridged"  # nothing cluster-wide
            self.w.vm = {"status": "Stopped", "devices": dict(body["devices"]), "config": body["config"],
                         "installed": False}
        elif path == "/1.0/instances/operations-center":
            self.w.act("move VM")
            assert self.w.vm["status"] == "Stopped", "a VM moves between pools only when stopped"
            assert body["pool"] in self.n["pools"]
            self.w.vm["devices"]["root"] = {**self.w.vm["devices"]["root"], "pool": body["pool"]}
        else:
            raise AssertionError(f"unexpected POST {path}")
        return {"id": "op"}

    def put(self, path: str, body=None):
        if path == "/1.0/instances/operations-center":
            self.w.act("remove install media")
            assert self.w.vm["status"] == "Stopped"
            self.w.vm["devices"] = body["devices"]
        elif path == "/os/1.0/system/provider":
            cfg = body["config"]["config"]
            assert "server_certificate" not in cfg, "nodes verify OC through the hitkit CA"
            assert cfg["server_url"] == f"https://{OC_IP}:8443"
            assert incus.fingerprint(self.w.oc_cert) != incus.fingerprint(_self_signed("x")[0])
            assert "BEGIN" in self.w.oc_cert and self.w.oc_issued, "OC needs its hitkit-CA certificate first"
            self.w.act(f"register {self.name}")
            self.n["registered"] = True
            self.w.servers[member(self.name)] = {"server_status": "ready", "cluster": ""}
        else:
            raise AssertionError(f"unexpected PUT {path}")
        return {"id": "op"}

    def patch(self, path: str, body=None):
        assert ("?target=" in path) == self._clustered(), "a clustered server's key is per member"
        new = body["config"]["storage.logs_volume"]
        if self.n["config"].get("storage.logs_volume") != new and self.w.vm and self.w.vm["status"] == "Running":
            raise incus.IncusError(400, "`storage.logs_volume` cannot be changed if there are running instances")
        self.w.act(f"logs volume {new}")
        self.n["config"]["storage.logs_volume"] = new

    def delete(self, path: str):
        *_, kind, name = path.rsplit("/", 2)
        if kind == "custom":
            self.w.act(f"delete volume {name}")
            if name not in self.n["volumes"]["bootstrap"]:
                raise incus.IncusError(404, "not found")
            assert self.n["config"].get("storage.logs_volume") != f"bootstrap/{name}", "that volume is in use"
            self.n["volumes"]["bootstrap"].discard(name)
        else:
            self.w.act("delete pool bootstrap")
            assert not self.n["volumes"]["bootstrap"], "the pool still has volumes"
            assert self.w.vm["devices"]["root"]["pool"] != "bootstrap", "the VM is on that pool"
            self.n["pools"].discard("bootstrap")

    def instance(self, name: str):
        return None if self.w.vm is None else {
            "status": self.w.vm["status"], "devices": dict(self.w.vm["devices"]), "config": self.w.vm["config"],
            "architecture": "x86_64", "ephemeral": False, "profiles": [], "stateful": False, "description": ""}

    def set_state(self, name: str, action: str, force: bool = False) -> None:
        self.w.act(f"{action} VM")
        self.w.vm["status"] = "Running" if action == "start" else "Stopped"
        if action == "start" and BOOT in self.w.vm["devices"]:
            self.w.vm["installed"] = True

    def console_log(self, name: str) -> str:
        assert self.w.vm["status"] == "Running"
        return "\x1b[5;1HIncusOS\x1b[5;9Hwas\x1b[5;13Hsuccessfully\x1b[5;26Hinstalled."

    def upload_iso(self, pool: str, name: str, path) -> None:
        self.w.act("upload ISO")
        assert path.exists()
        self.n["volumes"][pool].add(name)
        self.w.uploaded = name

    def trusted_clients(self):
        return []

    def pending_tokens(self):
        return []

    def revoke(self, name):
        return False

    def create_token(self, name, address):
        return {"token": f"token-for-{name}", "expires": None}


class OC:
    """The parts of OperationsCenterClient `finish` uses."""

    def __init__(self, world: World, pin: str):
        self.w = world
        if not world.oc_running():
            raise HitkitError("cannot reach Operations Center")
        if incus.fingerprint(world.oc_cert) != pin:
            raise incus.PinMismatch("Operations Center presented a certificate other than the pinned one")

    def authenticated(self):
        return True

    def has_update(self):
        return True

    def get(self, path):
        assert path == "/os/1.0/system/security"
        return {"config": {"encryption_recovery_keys": ["enc-oc"]}, "state": {"pool_recovery_keys": {"local": "pool-oc"}}}

    def set_certificate(self, certificate, key):
        self.w.act("OC certificate")
        self.w.oc_cert, self.w.oc_issued = certificate, True

    def registration_token(self, uses):
        return "token"

    def _database(self):
        if self.w.stalled_calls:
            self.w.stalled_calls -= 1
            raise incus.IncusError(500, "Internal server error, see the Operations Center log for details")

    def server(self, name):
        self._database()
        return self.w.servers.get(name)

    def cluster(self, name):
        self._database()
        return self.w.cluster and dict(self.w.cluster)

    def provisioned(self, name):
        return self.w.cluster["provisioned"]

    def create_cluster(self, name, url, servers, application_config):
        w = self.w
        node = w.nodes[FIRST]
        assert servers == [member(FIRST)], "only the node OC lives on: a joiner needs every pool"
        assert "local" not in node["pools"], "OpenTofu cannot create what exists"
        w.act("form cluster")
        w.cluster = {"members": list(servers), "certificate": _self_signed("cluster")[0], "provisioned": False}
        node["cert"] = w.cluster["certificate"]
        w.servers[member(FIRST)]["cluster"] = name
        node["pools"].add("local")
        logs = application_config["config"]["storage.logs_volume"]
        assert logs == node["config"]["storage.logs_volume"], "changing it fails: OC's own VM is running"
        w.cluster["provisioned"] = True

    def add_servers(self, name, servers):
        w = self.w
        assert "bootstrap" not in w.nodes[FIRST]["pools"], "a joining member needs every pool"
        w.act(f"join {servers}")
        for s in servers:
            w.cluster["members"].append(s)
            w.servers[s]["cluster"] = name
            w.nodes[s.split(".")[0]]["cert"] = w.cluster["certificate"]
        w.stalled_calls = w.stall_after_join

    def set_cluster_certificate(self, name, certificate, key):
        w = self.w
        self._database()
        if w.stalled_certificate_writes:
            w.stalled_certificate_writes -= 1
            raise incus.IncusError(500, "Internal server error, see the Operations Center log for details")
        w.act("cluster certificate")
        w.cluster["certificate"] = certificate
        for m in w.cluster["members"]:
            w.nodes[m.split(".")[0]]["cert"] = certificate


@pytest.fixture
def hctx(user):
    cfg = SimpleNamespace(install_id="test-install", network=lambda: SUBNET)
    return HostContext(user=user, cfg=cfg, ca=create_ca(SUBNET, "test-install"))


@pytest.fixture
def hooks():
    return kits.hooks(KIT)


@pytest.fixture
def plan():
    return kits.load(KIT).expand({"nodes": 1})


def connect(hooks, monkeypatch, world: World, tmp_path) -> None:
    mod = sys.modules[type(hooks).__module__]
    world.oc_issued = getattr(world, "oc_issued", False)
    monkeypatch.setattr(mod, "POLL", 0)
    for timeout in ("INSTALL_TIMEOUT", "BOOT_TIMEOUT", "UPDATE_TIMEOUT", "SERVER_TIMEOUT"):
        monkeypatch.setattr(mod, timeout, 0)  # the model answers at once: a wait that polls is a bug
    monkeypatch.setattr(mod, "SETTLE_TIMEOUT", 1)
    monkeypatch.setattr(incus, "IncusClient", lambda ip, c, k, pin, port: Replies(world, Node(world, IPS[ip], pin)))
    monkeypatch.setattr(mod, "OperationsCenterClient",
                        lambda ip, c, k, pin, port: Replies(world, OC(world, pin)))

    def presented(ip, port):
        if ip != OC_IP or not world.oc_running():
            raise OSError("refused")
        return world.oc_cert

    monkeypatch.setattr(incus, "server_certificate", presented)
    monkeypatch.setattr(images, "fetch_artifact", lambda *a: tmp_path / "iso.gz")

    def seeded_iso(src, dest, sections):
        world.seed = sections
        dest.parent.mkdir(parents=True, exist_ok=True)  # as the real download does
        dest.write_bytes(b"iso")

    monkeypatch.setattr(seed, "seeded_iso", seeded_iso)


def bring_up(hooks, hctx, world: World, plan) -> None:
    """What `hitkit up` does around the kit: probe and after_ready per machine, then finish."""
    for m in plan:
        for _ in range(3):  # the machine layer polls until it passes
            if hooks.probe_ready_for_test(hctx, m, world) is None:
                break
        else:
            pytest.fail(f"{m.name} never became ready")
        hooks.after_ready(hctx, m, plan, {}, lambda _m: None)
    hooks.finish(hctx, plan, {}, lambda _m: None)


@pytest.fixture(autouse=True)
def _pin_first_contact(hooks, monkeypatch):
    def probe(self, hctx, machine, world):
        if secrets.get(hctx.user, KIT, f"{machine.name}.server.crt") is None:
            secrets.put(hctx.user, KIT, f"{machine.name}.server.crt", world.nodes[machine.name]["cert"])
        return self.probe_ready(hctx, machine)

    monkeypatch.setattr(type(hooks), "probe_ready_for_test", probe, raising=False)


def fresh(hctx, hooks, plan):
    for m in plan:
        hooks.seed(hctx, m, plan)


def assert_finished(world: World, nodes: list[str]) -> None:
    assert world.vm["status"] == "Running"
    assert world.vm["devices"]["root"]["pool"] == "local"
    assert BOOT not in world.vm["devices"]
    assert world.nodes[FIRST]["pools"] == {"local"}
    assert world.nodes[FIRST]["config"]["storage.logs_volume"] == "local/logs"
    assert world.cluster["members"] == [member(n) for n in nodes]
    assert world.cluster["provisioned"]


# --- seed ----------------------------------------------------------------------------


def test_nodes_install_with_no_incus_defaults(hctx, hooks, plan):
    sections = hooks.seed(hctx, plan[0], plan)
    assert set(sections) == {"install", "network", "incus", "security"}
    assert sections["incus"]["apply_defaults"] is False  # OC's OpenTofu run creates them, and refuses to find them
    assert sections["incus"]["preseed"]["config"] == {"core.https_address": ":8443"}
    assert [c["name"] for c in sections["incus"]["preseed"]["certificates"]] == ["hitkit", "hitkit-browser"]
    iface = sections["network"]["interfaces"][0]
    assert iface["addresses"] == ["10.77.0.3/24"] and iface["hwaddr"] == "52:54:00:68:00:03"
    assert sections["network"]["dns"]["hostname"] == FIRST
    assert sections["security"]["custom_ca_certs"] == [hctx.ca.cert_pem.decode()]


def test_a_fresh_install_forgets_only_what_it_replaces(hctx, hooks, plan):
    three = kits.load(KIT).expand({"nodes": 3})
    for name in ("oc.crt", "cli.token", f"{SECOND}.server.crt", f"{FIRST}.server.crt", f"recovery.{FIRST}.pool-key"):
        secrets.put(hctx.user, KIT, name, "from-an-earlier-install")
    hooks.seed(hctx, three[1], three)
    assert secrets.get(hctx.user, KIT, f"{SECOND}.server.crt") is None
    assert secrets.get(hctx.user, KIT, "oc.crt") == "from-an-earlier-install"  # the first node's install holds OC
    hooks.seed(hctx, three[0], three)
    for name in ("oc.crt", "cli.token", f"{FIRST}.server.crt", f"recovery.{FIRST}.pool-key"):
        assert secrets.get(hctx.user, KIT, name) is None


# --- finish --------------------------------------------------------------------------


def test_one_node_comes_up_in_the_order_the_spike_proved(hctx, hooks, plan, monkeypatch, tmp_path):
    world = World()
    connect(hooks, monkeypatch, world, tmp_path)
    fresh(hctx, hooks, plan)
    bring_up(hooks, hctx, world, plan)
    assert_finished(world, [FIRST])
    assert world.log == [
        "create pool bootstrap", "create volume bootstrap/logs", "logs volume bootstrap/logs", "upload ISO",
        "create VM", "start VM", "stop VM", "remove install media", "start VM",
        "OC certificate", f"register {FIRST}", "form cluster",
        "stop VM", "logs volume local/logs", "move VM", "start VM",
        "delete volume operations-center.iso", "delete volume logs", "delete pool bootstrap",
        "cluster certificate",
    ]
    assert world.seed["applications"] == {"version": "1", "applications": [{"name": "operations-center"}]}
    assert "token" not in json.dumps(world.seed)  # nothing secret in a seed


def test_the_operations_center_seed_trusts_both_clients_and_uses_its_slot(hctx, hooks, plan, monkeypatch, tmp_path):
    world = World()
    connect(hooks, monkeypatch, world, tmp_path)
    fresh(hctx, hooks, plan)
    bring_up(hooks, hctx, world, plan)
    oc = world.seed["operations-center"]
    assert oc["trusted_client_certificates"] == [secrets.get(hctx.user, KIT, "client.crt"),
                                                 secrets.get(hctx.user, KIT, "browser.crt")]
    iface = world.seed["network"]["interfaces"][0]
    assert iface["addresses"] == [f"{OC_IP}/24"] and iface["roles"] == ["management"]
    assert iface["hwaddr"] == world.vm["devices"]["eth0"]["hwaddr"] == "52:54:00:68:00:06"


def test_a_second_up_changes_nothing(hctx, hooks, plan, monkeypatch, tmp_path):
    world = World()
    connect(hooks, monkeypatch, world, tmp_path)
    fresh(hctx, hooks, plan)
    bring_up(hooks, hctx, world, plan)
    world.log.clear()
    bring_up(hooks, hctx, world, plan)
    assert world.log == []


def test_nodes_join_only_after_bootstrap_is_gone_and_can_be_added_later(hctx, hooks, monkeypatch, tmp_path):
    world = World((FIRST, SECOND, THIRD))
    connect(hooks, monkeypatch, world, tmp_path)
    two = kits.load(KIT).expand({"nodes": 2})
    fresh(hctx, hooks, two)
    bring_up(hooks, hctx, world, two)
    assert_finished(world, [FIRST, SECOND])
    assert world.log.index("delete pool bootstrap") < world.log.index(f"join {[member(SECOND)]}")

    three = kits.load(KIT).expand({"nodes": 3})  # `--nodes 3`, on a cluster with its hitkit-CA certificate
    hooks.seed(hctx, three[2], three)
    bring_up(hooks, hctx, world, three)
    assert_finished(world, [FIRST, SECOND, THIRD])
    for n in (FIRST, SECOND, THIRD):
        assert secrets.get(hctx.user, KIT, f"{n}.server.crt") == secrets.get(hctx.user, KIT, "cluster.leaf.crt")


def count_ops(hooks, hctx, plan, monkeypatch, tmp_path) -> int:
    probe = World((FIRST, SECOND))
    connect(hooks, monkeypatch, probe, tmp_path)
    fresh(hctx, hooks, plan)
    bring_up(hooks, hctx, probe, plan)
    secrets.remove_kit(hctx.user, KIT)
    return probe.ops


@pytest.mark.parametrize("cut_after", [False, True], ids=["before-the-change", "after-the-change"])
def test_up_resumes_from_a_cut_at_every_single_change(hctx, hooks, monkeypatch, tmp_path, cut_after):
    two = kits.load(KIT).expand({"nodes": 2})
    total = count_ops(hooks, hctx, two, monkeypatch, tmp_path)
    assert total > 20
    for cut in range(1, total + 1):
        world = World((FIRST, SECOND))
        connect(hooks, monkeypatch, world, tmp_path)
        fresh(hctx, hooks, two)
        world.cut_at, world.cut_after = cut, cut_after
        with pytest.raises(CutOff):
            bring_up(hooks, hctx, world, two)
        bring_up(hooks, hctx, world, two)
        assert_finished(world, [FIRST, SECOND])
        secrets.remove_kit(hctx.user, KIT)


@pytest.mark.parametrize("stalled", [1, 2, 3, 4, 5])
def test_a_join_waits_out_operations_centers_busy_database(hctx, hooks, monkeypatch, tmp_path, stalled):
    # Live: after a join its resync held the database for 2.5 minutes, and calls failed with a 500.
    world = World((FIRST, SECOND))
    connect(hooks, monkeypatch, world, tmp_path)
    world.stall_after_join = stalled
    two = kits.load(KIT).expand({"nodes": 2})
    fresh(hctx, hooks, two)
    bring_up(hooks, hctx, world, two)
    assert_finished(world, [FIRST, SECOND])
    assert world.cluster["certificate"] == secrets.get(hctx.user, KIT, "cluster.leaf.crt")


def test_the_certificate_write_outlasts_a_resync_that_reads_got_past(hctx, hooks, plan, monkeypatch, tmp_path):
    world = World()
    connect(hooks, monkeypatch, world, tmp_path)
    world.stalled_certificate_writes = 3
    fresh(hctx, hooks, plan)
    bring_up(hooks, hctx, world, plan)
    assert world.cluster["certificate"] == secrets.get(hctx.user, KIT, "cluster.leaf.crt")


def test_scale_down_is_refused_before_anything_changes(hctx, hooks, monkeypatch, tmp_path):
    world = World((FIRST, SECOND))
    connect(hooks, monkeypatch, world, tmp_path)
    two = kits.load(KIT).expand({"nodes": 2})
    fresh(hctx, hooks, two)
    bring_up(hooks, hctx, world, two)
    world.log.clear()
    with pytest.raises(HitkitError, match=f"more members than --nodes asks for: {member(SECOND)}"):
        hooks.finish(hctx, two[:1], {}, lambda _m: None)
    assert world.log == []


def test_a_cluster_whose_setup_failed_says_so_instead_of_pretending(hctx, hooks, plan, monkeypatch, tmp_path):
    world = World()
    connect(hooks, monkeypatch, world, tmp_path)
    fresh(hctx, hooks, plan)
    bring_up(hooks, hctx, world, plan)
    world.cluster["provisioned"] = False  # OC lists it as ready either way
    with pytest.raises(HitkitError, match="did not finish, and it cannot be run twice"):
        hooks.finish(hctx, plan, {}, lambda _m: None)


def test_a_cluster_missing_its_vm_is_an_error_not_a_reinstall(hctx, hooks, plan, monkeypatch, tmp_path):
    world = World()
    connect(hooks, monkeypatch, world, tmp_path)
    fresh(hctx, hooks, plan)
    bring_up(hooks, hctx, world, plan)
    world.vm = None
    with pytest.raises(HitkitError, match="Operations Center VM is gone"):
        hooks.finish(hctx, plan, {}, lambda _m: None)


def test_a_reinstalled_member_is_refused_rather_than_registered_twice(hctx, hooks, monkeypatch, tmp_path):
    world = World((FIRST, SECOND))
    connect(hooks, monkeypatch, world, tmp_path)
    two = kits.load(KIT).expand({"nodes": 2})
    fresh(hctx, hooks, two)
    bring_up(hooks, hctx, world, two)
    world.servers[member(SECOND)]["cluster"] = ""
    world.nodes[SECOND].update(registered=False, cert=world.self_signed[SECOND])
    secrets.put(hctx.user, KIT, f"{SECOND}.server.crt", world.self_signed[SECOND])
    with pytest.raises(HitkitError, match="already knows incus-cluster-2"):
        hooks.finish(hctx, two, {}, lambda _m: None)


def test_probe_follows_a_node_that_clustered_as_the_last_up_was_cut_off(hctx, hooks, plan, monkeypatch, tmp_path):
    world = World()
    connect(hooks, monkeypatch, world, tmp_path)
    fresh(hctx, hooks, plan)
    bring_up(hooks, hctx, world, plan)
    secrets.put(hctx.user, KIT, f"{FIRST}.server.crt", world.self_signed[FIRST])  # the old pin
    assert "cluster's certificate" in hooks.probe_ready(hctx, plan[0])
    assert hooks.probe_ready(hctx, plan[0]) is None


def test_probe_still_refuses_a_certificate_nobody_vouches_for(hctx, hooks, plan, monkeypatch, tmp_path):
    world = World()
    connect(hooks, monkeypatch, world, tmp_path)
    fresh(hctx, hooks, plan)
    bring_up(hooks, hctx, world, plan)
    world.nodes[FIRST]["cert"] = _self_signed("someone-else")[0]
    with pytest.raises(incus.PinMismatch):
        hooks.probe_ready(hctx, plan[0])


# --- what you see --------------------------------------------------------------------


def test_creds_and_next_steps(hctx, hooks, plan, monkeypatch, tmp_path):
    with pytest.raises(HitkitError, match="not been brought up"):
        hooks.creds(hctx)
    world = World()
    connect(hooks, monkeypatch, world, tmp_path)
    fresh(hctx, hooks, plan)
    bring_up(hooks, hctx, world, plan)
    data = hooks.creds(hctx)
    assert data["url"] == "https://incus-cluster-1.hitkit.internal:8443"
    assert data["operations_center_url"] == "https://operations-center.hitkit.internal:8443"
    assert data["remote_add"] == "incus remote add hitkit-incus-cluster token-for-hitkit-incus-cluster-cli"
    assert data["incus_cluster_1_encryption_key"] == f"enc-{FIRST}"
    assert data["operations_center_pool_key"] == "pool-oc"
    steps = "\n".join(hooks.next_steps(hctx, plan))
    assert "incus remote add hitkit-incus-cluster" in steps and "operations-center.hitkit.internal" in steps


@pytest.mark.parametrize("name", ["hitkit", "hitkit-browser", "https://10.77.0.6:8443", "incus-cluster-2.hitkit.internal"])
def test_what_the_cluster_depends_on_cannot_be_revoked(hctx, hooks, name):
    with pytest.raises(HitkitError, match="lock hitkit out"):
        hooks.token_revoke(hctx, name)


def test_the_iso_pin_is_the_release_the_nodes_install(hooks):
    mod = sys.modules[type(hooks).__module__]
    iso, node = mod._iso_artifact(), images.pinned_artifacts(images.role_dirs("incus-os"))[0]
    assert iso.version == node.version
    assert iso.url.endswith(f"IncusOS_{iso.version}.iso.gz")
    assert len(iso.sha256) == 64


# --- the REST clients, over TLS -------------------------------------------------------


class FakeServer:
    """Answers from ``routes``: {(method, path): (status, kind, metadata)}, or bytes for a raw body."""

    def __init__(self, tmp_path, routes):
        self.cert, key = _self_signed("fake")
        (tmp_path / "fake.crt").write_text(self.cert)
        (tmp_path / "fake.key").write_text(key)
        self.seen: list[tuple[str, str, dict, bytes]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(size)
                fake.seen.append((self.command, self.path, dict(self.headers), body))
                answer = routes.get((self.command, self.path), (404, "error", None))
                if isinstance(answer, bytes):
                    raw, status = answer, 200
                else:
                    status, kind, metadata = answer
                    raw = json.dumps({"type": kind, "error": "" if status < 400 else "nope",
                                      "metadata": metadata}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(tmp_path / "fake.crt", tmp_path / "fake.key")
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self, cls, tmp_path):
        cert, key = _self_signed("client")
        (tmp_path / "client.crt").write_text(cert)
        (tmp_path / "client.key").write_text(key)
        return cls("127.0.0.1", tmp_path / "client.crt", tmp_path / "client.key", incus.fingerprint(self.cert),
                   self.server.server_port)


OP = "/1.0/operations/op1/wait?timeout=600"


def test_upload_iso_streams_the_file_with_incus_headers(tmp_path):
    fake = FakeServer(tmp_path, {
        ("POST", "/1.0/storage-pools/p/volumes/custom"): (202, "async", {"id": "op1"}),
        ("GET", OP): (200, "sync", {"status": "Success"}),
    })
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"\xfa" * 5000)
    fake.client(incus.IncusClient, tmp_path).upload_iso("p", "x.iso", iso)
    method, _, headers, body = fake.seen[0]
    assert (method, body) == ("POST", b"\xfa" * 5000)
    assert headers["X-Incus-name"] == "x.iso" and headers["X-Incus-type"] == "iso"
    assert headers["Content-Type"] == "application/octet-stream"


def test_a_failed_operation_raises_with_its_reason(tmp_path):
    fake = FakeServer(tmp_path, {("GET", OP): (200, "sync", {"status": "Failure", "err": "no space"})})
    with pytest.raises(HitkitError, match="no space"):
        fake.client(incus.IncusClient, tmp_path).wait({"id": "op1"})


def test_console_log_is_the_bare_text_and_instance_404_is_none(tmp_path):
    fake = FakeServer(tmp_path, {("GET", "/1.0/instances/vm/console?type=log"): b"boot text"})
    api = fake.client(incus.IncusClient, tmp_path)
    assert api.console_log("vm") == "boot text"
    assert api.instance("gone") is None


def test_operations_center_token_is_found_by_its_unique_description(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "hex_secret", lambda n: lambda: "abcd")
    fake = FakeServer(tmp_path, {
        ("POST", "/1.0/provisioning/tokens"): (201, "sync", None),
        ("GET", "/1.0/provisioning/tokens?recursion=1"): (200, "sync", [
            {"uuid": "other", "description": "someone else"}, {"uuid": "ours", "description": "hitkit-abcd"}]),
    })
    assert fake.client(OperationsCenterClient, tmp_path).registration_token(2) == "ours"
    sent = json.loads(fake.seen[0][3])
    assert sent["uses_remaining"] == 2 and sent["description"] == "hitkit-abcd"
    assert sent["expire_at"].endswith("Z")


def test_operations_center_says_tls_where_incus_says_trusted(tmp_path):
    fake = FakeServer(tmp_path, {("GET", "/1.0"): (200, "sync", {"auth": "tls"})})
    assert fake.client(OperationsCenterClient, tmp_path).authenticated() is True


def test_provisioned_means_the_terraform_artifact_exists(tmp_path):
    path = "/1.0/provisioning/clusters/c/artifacts"
    fake = FakeServer(tmp_path, {("GET", path): (200, "sync", [f"{path}/terraform-configuration"])})
    oc = fake.client(OperationsCenterClient, tmp_path)
    assert oc.provisioned("c") is True
    assert oc.provisioned("missing") is False
    assert oc.server("nobody") is None


def test_subnet_reaches_the_vm_and_the_seed(hctx, hooks, user):
    subnet = ipaddress.ip_network("10.78.0.0/24")
    cfg = SimpleNamespace(install_id="x", network=lambda: subnet)
    h = HostContext(user=user, cfg=cfg, ca=create_ca(subnet, "x"))
    assert hooks._vm_definition(h)["devices"]["eth0"]["hwaddr"] == "52:54:00:68:00:06"
    assert hooks._oc_seed(h)["network"]["interfaces"][0]["addresses"] == ["10.78.0.6/24"]
