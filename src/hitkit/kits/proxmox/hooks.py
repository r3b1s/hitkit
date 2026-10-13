"""Wiring for the proxmox kit.

hitkit generates the root password (credential), then over SSH: puts the
hitkit CA leaf on pveproxy (web UI and API on :8006 verify against the hitkit
CA), adds the data disk as `data` storage for guests, ISOs and backups, and
mints a privilege-separated-off API token for automation.
"""

from __future__ import annotations

import json
import shlex

from hitkit import secrets
from hitkit.errors import HitkitError
from hitkit.kits import Hooks
from hitkit.machine import remote

KIT = "proxmox"
TOKEN_ID = "hitkit"
URL = "https://proxmox.hitkit.internal:8006"


class KitHooks(Hooks):
    def credentials(self, hctx, machine, plan):
        pw = secrets.get_or_create(hctx.user, KIT, "root.password", secrets.password)
        return {"hitkit.pve.root-password": pw.encode()}

    def after_ready(self, hctx, machine, plan, knobs, log):
        run = lambda cmd: remote.run(hctx, machine.name, cmd)  # noqa: E731
        log("installing the hitkit certificate on pveproxy")
        # /etc/pve is pmxcfs: it fixes permissions itself and rejects chmod, so plain copies.
        run("cp /etc/hitkit/tls/server.crt /etc/pve/local/pveproxy-ssl.pem && "
            "cp /etc/hitkit/tls/server.key /etc/pve/local/pveproxy-ssl.key && "
            "systemctl restart pveproxy")
        # storage.cfg is authoritative; `pvesm status` can omit inactive storages.
        if run("grep -c '^dir: data$' /etc/pve/storage.cfg || true").strip() in ("", "0"):
            log("adding the data disk as Proxmox storage 'data'")
            run("install -d /srv/data/pve && pvesm add dir data --path /srv/data/pve "
                "--content images,rootdir,iso,vztmpl,backup,snippets")
        if secrets.get(hctx.user, KIT, "api.token") is None:
            log("minting API token root@pam!hitkit")
            run(f"pveum user token remove root@pam {TOKEN_ID} >/dev/null 2>&1 || true")
            out = run(f"pveum user token add root@pam {TOKEN_ID} --privsep 0 --output-format json")
            value = json.loads(out)["value"]
            secrets.put(hctx.user, KIT, "api.token", f"root@pam!{TOKEN_ID}={value}")

    def creds(self, hctx) -> dict:
        u = hctx.user
        token = secrets.get(u, KIT, "api.token")
        if token is None:
            raise HitkitError("the proxmox kit has not been brought up yet", hint="hitkit up proxmox")
        return {
            "url": URL,
            "username": "root@pam",
            "password": secrets.get(u, KIT, "root.password"),
            "api_token": token,
            "api_header": f"Authorization: PVEAPIToken={token}",
            "storage": "data (on the 100 GiB data disk)",
            "ssh": "hitkit ssh proxmox",
        }
