"""Wiring for the incus-cluster kit (docs/plans/incus-cluster.md).

Operations Center (OC) only manages clusters it formed itself, and it lives on the cluster
it forms, so the order matters; docs/incus/operations-center.md has the evidence for it:

1. The nodes install with no Incus defaults: OC's post-clustering OpenTofu run creates the
   `local` pool, the networks and the storage volumes, and fails on ones that already exist.
2. OC's VM goes on a temporary `bootstrap` pool on the first node, and that node's
   `storage.logs_volume` points at the same pool before the VM starts: Incus refuses to
   change that key while an instance runs, and OC cannot stop the VM it runs in.
3. OC gets a hitkit-CA certificate, so the nodes (which trust the hitkit CA from their seed)
   need no pin for it. The first node registers, and OC forms a cluster of that one node.
4. The VM stops, moves to `local`, and `bootstrap` goes: a joining member needs every pool.
5. The other nodes register and join. The cluster gets a hitkit-CA certificate.

`finish` runs these from the live state of the system, so an interrupted `up` resumes.
"""

from __future__ import annotations

import time
import tomllib
from pathlib import Path

from hitkit import incus, incusos, secrets, trace
from hitkit.addressing import DOMAIN, slot, slot_address
from hitkit.errors import HitkitError
from hitkit.host.ca import issue_leaf
from hitkit.machine import images, readiness, seed
from hitkit.machine.spec import mac_address
from hitkit.operations_center import OperationsCenterClient

KIT = CLUSTER = "incus-cluster"
REMOTE = "hitkit-incus-cluster"
BUNDLE_NAME = "hitkit incus-cluster"
FIRST = "incus-cluster-1"
NODES = tuple(f"{KIT}-{i}" for i in range(1, 4))  # every slot, for a certificate that outlasts `--nodes`
OC = "operations-center"  # its address slot and its instance name
OC_LIMITS = {"limits.cpu": "2", "limits.memory": "4GiB"}
OC_DISK = "50GiB"  # IncusOS's minimum
BOOTSTRAP = "bootstrap"
ISO_VOLUME = "operations-center.iso"
BOOT_MEDIA = "boot-media"
# The spike's longest measured times, on a busy host, were 6, 7 and 5 minutes.
INSTALL_TIMEOUT = BOOT_TIMEOUT = UPDATE_TIMEOUT = 1500
SERVER_TIMEOUT = 300
SETTLE_TIMEOUT = 600  # the longest resync seen took 2.5 minutes
POLL = 5
# Secrets that belong to one install: a fresh install of the first node starts without them.
PER_CLUSTER = ("oc.crt", "oc.leaf.crt", "oc.leaf.key", "cluster.leaf.crt", "cluster.leaf.key",
               "recovery.operations-center.encryption-key", "recovery.operations-center.pool-key",
               "cli.token", "cli.token-used")

_t = trace.logger(__name__)


def _pin(node: str) -> str:
    return f"{node}.server.crt"


def _member(node: str) -> str:
    return slot(node).fqdn()  # OC and Incus name a server by its host name


def _until(check, what: str, timeout: float) -> None:
    """Poll ``check`` until it is true. A server that is not answering yet is not a failure."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if check():
                return
            reason = "not yet"
        except HitkitError as e:
            reason = str(e)
        if time.monotonic() >= deadline:
            raise HitkitError(f"timed out waiting for {what} ({reason})")
        time.sleep(POLL)


def _iso_artifact() -> images.Artifact:
    raw = tomllib.loads((Path(__file__).parent / "operations-center.toml").read_text())["iso"]
    return images.Artifact(name=raw["name"], version=str(raw["version"]), url=raw["url"], sha256=raw["sha256"],
                           dest="", mode=0o644)


class KitHooks(incusos.TrustHooks):
    kit, remote, bundle_name, up_secret = KIT, REMOTE, BUNDLE_NAME, _pin(FIRST)
    host = slot(FIRST).fqdn()

    def protected(self, name: str) -> bool:
        # OC's client certificate is named by its URL, a member's server certificate by its host name.
        return super().protected(name) or name.startswith("https://") or name.endswith(f".{DOMAIN}")

    def _client_files(self, hctx) -> tuple[Path, Path]:
        incusos.keypair(hctx.user, KIT, "client", "hitkit")
        d = secrets.kit_dir(hctx.user, KIT)
        return d / "client.crt", d / "client.key"

    def _ip(self, hctx, name: str) -> str:
        return str(slot_address(hctx.cfg.network(), name))

    def _node(self, hctx, name: str, pem: str | None = None) -> incus.IncusClient:
        pem = pem or secrets.get(hctx.user, KIT, _pin(name))
        if pem is None:
            raise HitkitError("the incus-cluster kit has not been brought up yet", hint="hitkit up incus-cluster")
        crt, key = self._client_files(hctx)
        return incus.IncusClient(self._ip(hctx, name), crt, key, incus.fingerprint(pem), incus.PORT)

    def _oc(self, hctx) -> OperationsCenterClient:
        pem = secrets.get(hctx.user, KIT, "oc.crt")
        if pem is None:
            raise HitkitError("Operations Center has not been installed yet", hint="hitkit up incus-cluster")
        crt, key = self._client_files(hctx)
        return OperationsCenterClient(self._ip(hctx, OC), crt, key, incus.fingerprint(pem), incus.PORT)

    def api_client(self, hctx) -> incus.IncusClient:
        return self._node(hctx, FIRST)

    def seed(self, hctx, machine, plan):
        u = hctx.user
        # An earlier install's pin and keys are void now.
        for name in (_pin(machine.name), f"recovery.{machine.name}.encryption-key", f"recovery.{machine.name}.pool-key"):
            secrets.delete(u, KIT, name)
        if machine.name == FIRST:
            for name in PER_CLUSTER:
                secrets.delete(u, KIT, name)
        client_cert, _ = incusos.keypair(u, KIT, "client", "hitkit")
        browser_cert, _ = incusos.keypair(u, KIT, "browser", "hitkit-browser")
        incusos.browser_bundle(u, KIT, BUNDLE_NAME)
        return {
            "install": {"version": "1", "force_reboot": False, "target": {"id": "hitkit-data"}},
            "network": incusos.network_seed(hctx.cfg.network(), machine.name, ["management", "instances"]),
            "incus": {
                "version": "1", "apply_defaults": False,
                "preseed": {"config": {"core.https_address": f":{incus.PORT}"}, "certificates": [
                    {"name": "hitkit", "type": "client", "certificate": client_cert},
                    {"name": "hitkit-browser", "type": "client", "certificate": browser_cert},
                ]},
            },
            "security": incusos.security_seed(hctx.ca.cert_pem),
        }

    def probe_ready(self, hctx, machine):
        name, ip = machine.name, self._ip(hctx, machine.name)
        pem = secrets.get(hctx.user, KIT, _pin(name))
        if pem is None:
            try:
                pem = incus.server_certificate(ip, incus.PORT)
            except OSError as e:
                return f"no TLS answer on {ip}:{incus.PORT} yet ({e})"
            # Trust on first use, over the host-only bridge seconds after the install (design §5).
            secrets.put(hctx.user, KIT, _pin(name), pem)
            _t.info("pinned the server certificate", machine=name, fingerprint=incus.fingerprint(pem))
        try:
            auth = (self._node(hctx, name, pem).get("/1.0") or {}).get("auth")
        except incus.PinMismatch:
            if self._refresh_pin(hctx, name, pem):  # an `up` was cut off just after this node clustered
                return "the node has the cluster's certificate now"
            raise
        return None if auth == "trusted" else f"the server does not trust hitkit's client certificate (auth: {auth})"

    def _refresh_pin(self, hctx, name: str, pem: str) -> bool:
        """Pin the certificate OC holds for the cluster, if this node is a member and it is news."""
        try:
            oc = self._oc(hctx)
            server, cluster = oc.server(_member(name)), oc.cluster(CLUSTER)
        except HitkitError:
            return False
        current = cluster and cluster.get("certificate")
        if not (server and server.get("cluster") == CLUSTER and current) or current == pem:
            return False
        secrets.put(hctx.user, KIT, _pin(name), current)
        return True

    def after_ready(self, hctx, machine, plan, knobs, log):
        incusos.store_recovery_keys(hctx.user, KIT, self._node(hctx, machine.name), log,
                                    prefix=f"recovery.{machine.name}", label=f"{machine.name} ")

    def finish(self, hctx, plan, knobs, log):
        wanted = [m.name for m in plan]
        self._refuse_scale_down(hctx, wanted)
        self._ensure_vm(hctx, log)
        oc = self._ensure_oc(hctx, log)
        self._ensure_cluster(hctx, oc, log)
        self._ensure_vm_on_local(hctx, log)
        oc = self._wait_oc(hctx)
        incusos.store_recovery_keys(hctx.user, KIT, oc, log, prefix="recovery.operations-center",
                                    label="Operations Center ")
        self._ensure_members(hctx, oc, wanted, log)
        self._ensure_cluster_certificate(hctx, oc, wanted, log)
        self.ensure_cli_token(hctx, self._node(hctx, FIRST), log)

    def _refuse_scale_down(self, hctx, wanted: list[str]) -> None:
        node = self._node(hctx, FIRST)
        if not (node.get("/1.0") or {}).get("environment", {}).get("server_clustered"):
            return
        members = {m.rsplit("/", 1)[-1] for m in node.get("/1.0/cluster/members") or []}
        extra = sorted(members - {_member(n) for n in wanted})
        if extra:
            raise HitkitError(f"the cluster has more members than --nodes asks for: {', '.join(extra)}",
                              hint="removing members is not supported; `hitkit destroy incus-cluster --delete-data` "
                                   "starts over")

    # --- the Operations Center VM --------------------------------------------------------

    def _ensure_vm(self, hctx, log) -> None:
        node = self._node(hctx, FIRST)
        vm = node.instance(OC)
        if vm is None:
            if (node.get("/1.0") or {}).get("environment", {}).get("server_clustered"):
                raise HitkitError("the cluster exists but the Operations Center VM is gone",
                                  hint="`hitkit destroy incus-cluster --delete-data` starts over")
            log("creating the Operations Center VM")
            self._prepare_bootstrap(hctx, node, log)
            node.wait(node.post("/1.0/instances", self._vm_definition(hctx)))
            vm = node.instance(OC)
        if BOOT_MEDIA in vm["devices"]:
            self._install(hctx, node, vm, log)
            vm = node.instance(OC)
        if vm["status"] != "Running":
            node.set_state(OC, "start")

    def _prepare_bootstrap(self, hctx, node: incus.IncusClient, log) -> None:
        pools = {p.rsplit("/", 1)[-1] for p in node.get("/1.0/storage-pools") or []}
        if BOOTSTRAP not in pools:
            node.post("/1.0/storage-pools", {"name": BOOTSTRAP, "driver": "zfs",
                                             "config": {"source": f"local/{BOOTSTRAP}"}})
        volumes = {v.rsplit("/", 1)[-1] for v in node.get(f"/1.0/storage-pools/{BOOTSTRAP}/volumes/custom") or []}
        if "logs" not in volumes:
            node.post(f"/1.0/storage-pools/{BOOTSTRAP}/volumes/custom",
                      {"name": "logs", "type": "custom", "config": {}})
        node.patch("/1.0", {"config": {"storage.logs_volume": f"{BOOTSTRAP}/logs"}})
        if ISO_VOLUME not in volumes:
            self._upload_iso(hctx, node, log)

    def _upload_iso(self, hctx, node: incus.IncusClient, log) -> None:
        src = images.fetch_artifact(hctx.user, _iso_artifact(), log)
        iso = hctx.user.cache_dir / "downloads" / f"{KIT}-seeded.iso"
        try:
            seed.seeded_iso(src, iso, self._oc_seed(hctx))
            log("uploading the Operations Center install image")
            node.upload_iso(BOOTSTRAP, ISO_VOLUME, iso)
        finally:
            iso.unlink(missing_ok=True)

    def _oc_seed(self, hctx) -> dict:
        client_cert, _ = incusos.keypair(hctx.user, KIT, "client", "hitkit")
        browser_cert, _ = incusos.keypair(hctx.user, KIT, "browser", "hitkit-browser")
        return {
            "install": {"version": "1", "force_install": False, "force_reboot": False},
            "applications": {"version": "1", "applications": [{"name": OC}]},
            "network": incusos.network_seed(hctx.cfg.network(), OC, ["management"]),
            "operations-center": {"version": "1", "apply_defaults": True, "preseed": None,
                                  "trusted_client_certificates": [client_cert, browser_cert]},
            "security": incusos.security_seed(hctx.ca.cert_pem),
        }

    def _vm_definition(self, hctx) -> dict:
        subnet = hctx.cfg.network()
        return {
            "name": OC, "type": "virtual-machine", "source": {"type": "none"},
            # secureboot=false only leaves out Microsoft's keys, so IncusOS can enroll its own.
            "config": {"security.secureboot": "false", "boot.autostart": "true", **OC_LIMITS},
            "devices": {
                "root": {"type": "disk", "path": "/", "pool": BOOTSTRAP, "size": OC_DISK},
                "vtpm": {"type": "tpm"},
                # An unmanaged bridge: nothing cluster-wide depends on it, and OC's terraform leaves it be.
                "eth0": {"type": "nic", "nictype": "bridged", "parent": "uplink",
                         "hwaddr": mac_address(subnet, slot(OC).host_octet)},
                BOOT_MEDIA: {"type": "disk", "pool": BOOTSTRAP, "source": ISO_VOLUME, "boot.priority": "10"},
            },
        }

    def _install(self, hctx, node: incus.IncusClient, vm: dict, log) -> None:
        if vm["status"] != "Running":
            node.set_state(OC, "start")
        log("installing Operations Center (nested, this takes several minutes)")
        _until(lambda: readiness.scan_installer(node.console_log(OC)), "the Operations Center install", INSTALL_TIMEOUT)
        node.set_state(OC, "stop", force=True)
        vm = node.instance(OC)
        devices = {k: v for k, v in vm["devices"].items() if k != BOOT_MEDIA}
        node.wait(node.put(f"/1.0/instances/{OC}", {
            "architecture": vm["architecture"], "config": vm["config"], "devices": devices,
            "ephemeral": vm["ephemeral"], "profiles": vm["profiles"], "stateful": vm["stateful"],
            "description": vm["description"]}))

    def _wait_oc(self, hctx) -> OperationsCenterClient:
        oc = self._oc(hctx)
        _until(oc.authenticated, "Operations Center to answer", BOOT_TIMEOUT)
        return oc

    def _leaf(self, hctx, name: str, dns: list[str], ips: list[str]) -> tuple[str, str]:
        """``<name>.leaf.crt`` and ``.key``: a hitkit-CA certificate, generated once. Returns (cert, key)."""
        u = hctx.user
        cert, key = secrets.get(u, KIT, f"{name}.leaf.crt"), secrets.get(u, KIT, f"{name}.leaf.key")
        if cert is None or key is None:
            key_pem, cert_pem = issue_leaf(hctx.ca, dns, ips)
            key, cert = key_pem.decode(), cert_pem.decode()
            secrets.put(u, KIT, f"{name}.leaf.key", key)
            secrets.put(u, KIT, f"{name}.leaf.crt", cert)
        return cert, key

    def _ensure_oc(self, hctx, log) -> OperationsCenterClient:
        """OC answering with its hitkit-CA certificate, and with an update cached."""
        u, ip = hctx.user, self._ip(hctx, OC)
        log("waiting for Operations Center to start")
        leaf, key = self._leaf(hctx, "oc", [slot(OC).fqdn()], [slot_address(hctx.cfg.network(), OC)])

        def answering_with_a_pin() -> bool:
            try:
                presented = incus.server_certificate(ip, incus.PORT)
            except OSError:
                return False
            if incus.fingerprint(presented) == incus.fingerprint(leaf):  # an earlier `up` got this far
                secrets.put(u, KIT, "oc.crt", leaf)
            elif secrets.get(u, KIT, "oc.crt") is None:  # trust on first use, as for the nodes
                secrets.put(u, KIT, "oc.crt", presented)
            return True

        _until(answering_with_a_pin, "Operations Center to start", BOOT_TIMEOUT)
        oc = self._wait_oc(hctx)
        if secrets.get(u, KIT, "oc.crt") != leaf:
            oc.set_certificate(leaf, key)
            secrets.put(u, KIT, "oc.crt", leaf)
            oc = self._wait_oc(hctx)
        if not oc.has_update():
            log("waiting for Operations Center to fetch its first update")
            _until(oc.has_update, "Operations Center's first update", UPDATE_TIMEOUT)
        return oc

    # --- the cluster ---------------------------------------------------------------------

    def _register(self, hctx, oc: OperationsCenterClient, node: str, token: str) -> None:
        """Register an installed server. It verifies OC through the hitkit CA from its seed."""
        api = self._node(hctx, node)
        if api.get("/os/1.0/system/provider")["state"]["registered"]:
            return
        if oc.server(_member(node)):
            raise HitkitError(f"Operations Center already knows {node} from an earlier install",
                              hint="`hitkit destroy incus-cluster --delete-data` starts over")
        api.put("/os/1.0/system/provider", {"config": {"name": OC, "config": {
            "server_url": f"https://{self._ip(hctx, OC)}:{incus.PORT}", "server_token": token}}})

    def _wait_server(self, oc: OperationsCenterClient, node: str) -> None:
        _until(lambda: (oc.server(_member(node)) or {}).get("server_status") == "ready",
               f"{node} to be ready in Operations Center", SERVER_TIMEOUT)

    def _pin_cluster(self, hctx, oc: OperationsCenterClient, nodes: list[str]) -> None:
        pem = oc.cluster(CLUSTER)["certificate"]
        for node in nodes:
            secrets.put(hctx.user, KIT, _pin(node), pem)

    def _settle(self, oc: OperationsCenterClient) -> None:
        """After clustering or a join OC resyncs its inventory, and until that ends its database
        calls stall or fail with a 500 (docs/incus/operations-center.md §5)."""
        _until(lambda: oc.cluster(CLUSTER) is not None, "Operations Center to settle", SETTLE_TIMEOUT)

    def _ensure_cluster(self, hctx, oc: OperationsCenterClient, log) -> None:
        if oc.cluster(CLUSTER) is None:
            self._register(hctx, oc, FIRST, oc.registration_token(1))
            self._wait_server(oc, FIRST)
            log("Operations Center is forming the cluster")
            oc.create_cluster(CLUSTER, f"https://{self._ip(hctx, FIRST)}:{incus.PORT}", [_member(FIRST)],
                              # The same value as the node's: it cannot change while OC's own VM runs.
                              {"config": {"storage.logs_volume": f"{BOOTSTRAP}/logs"}})
        self._settle(oc)
        if not oc.provisioned(CLUSTER):
            raise HitkitError("Operations Center's cluster setup did not finish, and it cannot be run twice",
                              hint="`hitkit trace --failed last` has the error; "
                                   "`hitkit destroy incus-cluster --delete-data` starts over")
        self._pin_cluster(hctx, oc, [FIRST])

    def _ensure_vm_on_local(self, hctx, log) -> None:
        node = self._node(hctx, FIRST)
        vm = node.instance(OC)
        if vm["devices"]["root"]["pool"] == BOOTSTRAP:
            log("moving the Operations Center VM to the local pool")
            node.set_state(OC, "stop")
            node.patch(f"/1.0?target={_member(FIRST)}", {"config": {"storage.logs_volume": "local/logs"}})
            node.wait(node.post(f"/1.0/instances/{OC}", {"name": OC, "migration": True, "pool": "local"}))
            node.set_state(OC, "start")
        elif vm["status"] != "Running":
            node.set_state(OC, "start")
        if f"/1.0/storage-pools/{BOOTSTRAP}" in (node.get("/1.0/storage-pools") or []):
            for volume in (ISO_VOLUME, "logs"):
                try:
                    node.delete(f"/1.0/storage-pools/{BOOTSTRAP}/volumes/custom/{volume}")
                except incus.IncusError as e:
                    if e.status != 404:  # gone already: an earlier `up` was cut off here
                        raise
            node.delete(f"/1.0/storage-pools/{BOOTSTRAP}")

    def _ensure_members(self, hctx, oc: OperationsCenterClient, wanted: list[str], log) -> None:
        self._settle(oc)
        joining = [n for n in wanted[1:] if (oc.server(_member(n)) or {}).get("cluster") != CLUSTER]
        if not joining:
            return
        token = oc.registration_token(len(joining))
        for node in joining:
            self._register(hctx, oc, node, token)
            self._wait_server(oc, node)
        log(f"joining {', '.join(joining)} to the cluster")
        oc.add_servers(CLUSTER, [_member(n) for n in joining])
        log("waiting for Operations Center to take in the new members")
        self._settle(oc)
        self._pin_cluster(hctx, oc, joining)

    def _ensure_cluster_certificate(self, hctx, oc: OperationsCenterClient, wanted: list[str], log) -> None:
        subnet = hctx.cfg.network()
        leaf, key = self._leaf(hctx, "cluster", [slot(n).fqdn() for n in NODES],
                               [slot_address(subnet, n) for n in NODES])
        if oc.cluster(CLUSTER)["certificate"].strip() != leaf.strip():
            log("giving the cluster a hitkit-CA certificate")
            _until(lambda: oc.set_cluster_certificate(CLUSTER, leaf, key) is None,
                   "the cluster's certificate", SETTLE_TIMEOUT)  # the same one again is harmless
        self._pin_cluster(hctx, oc, wanted)

    # --- what you see ---------------------------------------------------------------------

    def next_steps(self, hctx, plan):
        token = secrets.get(hctx.user, KIT, "cli.token")
        if token:
            lines = ["Add it to your incus CLI:",
                     f"  incus remote add {REMOTE} {token}",
                     f"  incus launch images:debian/trixie {REMOTE}:test"]
        else:
            lines = [f"Your incus CLI already has it (the token was used): incus list {REMOTE}:"]
        return lines + [f"Cluster UI: {self.url()}  (log in with your certificate: hitkit browser-cert incus-cluster)",
                        f"Operations Center: https://{slot(OC).fqdn()}:{incus.PORT}  (the same certificate)",
                        "More tokens: hitkit token create incus-cluster --name <name>"]

    def creds(self, hctx) -> dict:
        u = hctx.user
        pem = self.require_up(hctx)
        bundle, password = incusos.browser_bundle(u, KIT, BUNDLE_NAME)
        data = {"url": self.url(), "ui": f"{self.url()}/ui/", "server_fingerprint": incus.fingerprint(pem),
                "operations_center_url": f"https://{slot(OC).fqdn()}:{incus.PORT}"}
        if token := secrets.get(u, KIT, "cli.token"):
            data["remote_add"] = f"incus remote add {REMOTE} {token}"
        data.update({
            "ui_certificate": bundle,
            "ui_certificate_password": password,
            "ui_import": "hitkit browser-cert incus-cluster (imports it into Firefox and Chromium; then restart them)",
        })
        for name in (*NODES, OC):
            for kind in ("encryption-key", "pool-key"):
                if key := secrets.get(u, KIT, f"recovery.{name}.{kind}"):
                    data[f"{name}_{kind}".replace("-", "_")] = key
        data["shell"] = f"none: use incus / incus admin os against the {REMOTE} remote"
        return data
