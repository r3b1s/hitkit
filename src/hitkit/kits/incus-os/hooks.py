"""Wiring for the incus-os kit (docs/plans/incus-os.md).

IncusOS is installed from its vendor's image and has no shell, so everything goes
through the Incus API. The install seed makes the server trust two client certificates
hitkit generates: its own, and one for your browser (the web UI). hitkit pins the
server's self-signed certificate on first contact (design §5), stores the disk recovery
keys, and hands out trust tokens for `incus remote add`.
"""

from __future__ import annotations

from hitkit import incus, incusos, secrets, trace
from hitkit.addressing import slot, slot_address

KIT = MACHINE = "incus-os"
REMOTE = "hitkit-incus-os"
BUNDLE_NAME = "hitkit incus-os"
# Secrets that belong to one install of the server: a fresh install starts without them.
PER_INSTALL = ("server.crt", "cli.token", "cli.token-used", "recovery.encryption-key", "recovery.pool-key")

_t = trace.logger(__name__)


class KitHooks(incusos.TrustHooks):
    kit, remote, bundle_name, up_secret = KIT, REMOTE, BUNDLE_NAME, "server.crt"
    host = slot(MACHINE).fqdn()

    def seed(self, hctx, machine, plan):
        u = hctx.user
        for name in PER_INSTALL:  # an earlier install's pin, tokens and keys are void now
            secrets.delete(u, KIT, name)
        client_cert, _ = incusos.keypair(u, KIT, "client", "hitkit")
        browser_cert, _ = incusos.keypair(u, KIT, "browser", "hitkit-browser")
        incusos.browser_bundle(u, KIT, BUNDLE_NAME)
        # The files that worked in the spike: docs/incus/incus-os-in-libvirt.md §2.
        return {
            "install": {"version": "1", "force_reboot": False, "target": {"id": "hitkit-data"}},
            "network": incusos.network_seed(hctx.cfg.network(), machine.name, ["management", "instances"]),
            "incus": {
                "version": "1", "apply_defaults": True,
                "preseed": {"certificates": [
                    {"name": "hitkit", "type": "client", "certificate": client_cert},
                    {"name": "hitkit-browser", "type": "client", "certificate": browser_cert},
                ]},
            },
            "security": incusos.security_seed(hctx.ca.cert_pem),
        }

    def api_client(self, hctx, server_pem: str | None = None) -> incus.IncusClient:
        pem = server_pem or self.require_up(hctx)
        incusos.keypair(hctx.user, KIT, "client", "hitkit")
        d = secrets.kit_dir(hctx.user, KIT)
        ip = str(slot_address(hctx.cfg.network(), MACHINE))
        return incus.IncusClient(ip, d / "client.crt", d / "client.key", incus.fingerprint(pem), incus.PORT)

    def probe_ready(self, hctx, machine):
        ip = str(slot_address(hctx.cfg.network(), machine.name))
        pem = secrets.get(hctx.user, KIT, "server.crt")
        if pem is None:
            try:
                pem = incus.server_certificate(ip, incus.PORT)
            except OSError as e:
                return f"no TLS answer on {ip}:{incus.PORT} yet ({e})"
            # Trust on first use, over the host-only bridge seconds after the install (design §5).
            secrets.put(hctx.user, KIT, "server.crt", pem)
            _t.info("pinned the server certificate", machine=machine.name, fingerprint=incus.fingerprint(pem))
        auth = (self.api_client(hctx, pem).get("/1.0") or {}).get("auth")
        return None if auth == "trusted" else f"the server does not trust hitkit's client certificate (auth: {auth})"

    def after_ready(self, hctx, machine, plan, knobs, log):
        api = self.api_client(hctx)
        incusos.store_recovery_keys(hctx.user, KIT, api, log)
        self.ensure_cli_token(hctx, api, log)

    def next_steps(self, hctx, plan):
        token = secrets.get(hctx.user, KIT, "cli.token")
        if token:
            lines = ["Add it to your incus CLI:",
                     f"  incus remote add {REMOTE} {token}",
                     f"  incus launch images:debian/trixie {REMOTE}:test"]
        else:
            lines = [f"Your incus CLI already has it (the token was used): incus list {REMOTE}:"]
        return lines + [f"Web UI: {self.url()}  (log in with your certificate: hitkit browser-cert incus-os)",
                        "More tokens: hitkit token create incus-os --name <name>"]

    def creds(self, hctx) -> dict:
        u = hctx.user
        pem = self.require_up(hctx)
        bundle, password = incusos.browser_bundle(u, KIT, BUNDLE_NAME)
        data = {"url": self.url(), "ui": f"{self.url()}/ui/", "server_fingerprint": incus.fingerprint(pem)}
        if token := secrets.get(u, KIT, "cli.token"):
            data["remote_add"] = f"incus remote add {REMOTE} {token}"
        data.update({
            "ui_certificate": bundle,
            "ui_certificate_password": password,
            "ui_import": "hitkit browser-cert incus-os (imports it into Firefox and Chromium; then restart them)",
            "encryption_recovery_key": secrets.get(u, KIT, "recovery.encryption-key"),
            "pool_recovery_key": secrets.get(u, KIT, "recovery.pool-key"),
            "shell": f"none: use incus / incus admin os against the {REMOTE} remote",
        })
        return data
