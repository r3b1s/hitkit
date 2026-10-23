"""Wiring for the pbs kit.

hitkit generates the root password (credential), then over SSH: puts the hitkit CA
leaf on proxmox-backup-proxy (web UI and API on :8007 verify against the hitkit CA),
creates the datastore `data` on the data disk, and mints an admin API token. With
`--proxmox` it also creates a backup-only user and token for Proxmox VE and adds the
datastore there as storage `pbs`; that step is repeatable and refreshes the token.
"""

from __future__ import annotations

import json

from hitkit import secrets
from hitkit.errors import HitkitError
from hitkit.kits import Hooks
from hitkit.machine import remote

KIT = "pbs"
SERVER = "pbs.hitkit.internal"
URL = f"https://{SERVER}:8007"
DATASTORE = "data"
DATASTORE_PATH = "/srv/data/pbs"
TOKEN_NAME = "hitkit"
ADMIN_TOKEN_ID = f"root@pam!{TOKEN_NAME}"
PVE_USER = "pve@pbs"
PVE_TOKEN_ID = f"{PVE_USER}!{TOKEN_NAME}"
PVE_STORAGE = "pbs"


class KitHooks(Hooks):
    def credentials(self, hctx, machine, plan):
        pw = secrets.get_or_create(hctx.user, KIT, "root.password", secrets.password)
        return {"hitkit.pbs.root-password": pw.encode()}

    def after_ready(self, hctx, machine, plan, knobs, log):
        run = lambda cmd, stdin=None: remote.run(hctx, machine.name, cmd, stdin)  # noqa: E731
        log("installing the hitkit certificate on proxmox-backup-proxy")
        run("install -m 0640 -o root -g backup /etc/hitkit/tls/server.crt /etc/proxmox-backup/proxy.pem && "
            "install -m 0640 -o root -g backup /etc/hitkit/tls/server.key /etc/proxmox-backup/proxy.key && "
            "systemctl restart proxmox-backup-proxy")
        stores = json.loads(run("proxmox-backup-manager datastore list --output-format json") or "[]")
        if DATASTORE not in {s["name"] for s in stores}:
            log(f"creating datastore '{DATASTORE}' on the data disk")
            run(f"install -d -o backup -g backup {DATASTORE_PATH} && "
                f"proxmox-backup-manager datastore create {DATASTORE} {DATASTORE_PATH}")
        if secrets.get(hctx.user, KIT, "api.token") is None:
            log(f"minting API token {ADMIN_TOKEN_ID}")
            value = self._mint(run, "root@pam")
            run(f"proxmox-backup-manager acl update / Admin --auth-id '{ADMIN_TOKEN_ID}'")
            secrets.put(hctx.user, KIT, "api.token", f"{ADMIN_TOKEN_ID}:{value}")

    def finish(self, hctx, plan, knobs, log):
        if knobs.get("proxmox"):
            self._add_to_proxmox(hctx, log)

    @staticmethod
    def _mint(run, user: str) -> str:
        run(f"proxmox-backup-manager user delete-token {user} {TOKEN_NAME} >/dev/null 2>&1 || true")
        out = run(f"proxmox-backup-manager user generate-token {user} {TOKEN_NAME}")
        # No --output-format here: the command prints "Result: {json}".
        return json.loads(out[out.index("{"):out.rindex("}") + 1])["value"]

    def _add_to_proxmox(self, hctx, log) -> None:
        """Give Proxmox VE a backup-only user on the datastore and add it as storage."""
        u = hctx.user
        if secrets.get(u, "proxmox", "api.token") is None:
            raise HitkitError("--proxmox needs the proxmox kit, which has not been brought up",
                              hint="hitkit up proxmox, then hitkit up pbs --proxmox")
        pbs = lambda cmd: remote.run(hctx, KIT, cmd)  # noqa: E731
        acl = lambda auth_id: pbs(  # noqa: E731
            f"proxmox-backup-manager acl update /datastore/{DATASTORE} DatastorePowerUser --auth-id '{auth_id}'")
        # A token's permissions are capped by its user's, so the user needs the ACL too.
        # Every step is idempotent and runs each time, so a half-finished earlier run heals.
        pbs(f"proxmox-backup-manager user list --output-format json | grep -q '\"{PVE_USER}\"' || "
            f"proxmox-backup-manager user create {PVE_USER} --comment 'hitkit: Proxmox VE backups'")
        acl(PVE_USER)
        if secrets.get(u, KIT, "pve.token") is None:
            log(f"creating backup-only token {PVE_TOKEN_ID} for Proxmox VE")
            secrets.put(u, KIT, "pve.token", self._mint(pbs, PVE_USER))
        acl(PVE_TOKEN_ID)
        secret = secrets.get(u, KIT, "pve.token")
        have = remote.run(hctx, "proxmox", f"grep -c '^pbs: {PVE_STORAGE}$' /etc/pve/storage.cfg || true")
        verb = "set" if have.strip() not in ("", "0") else "add pbs"
        log(f"{'refreshing' if verb == 'set' else 'adding'} Proxmox VE storage '{PVE_STORAGE}'")
        # The token secret goes in on stdin, never in this host's argv.
        remote.run(
            hctx, "proxmox",
            f"read -r secret && pvesm {verb} {PVE_STORAGE} --server {SERVER} --datastore {DATASTORE} "
            f"--username '{PVE_TOKEN_ID}' --password \"$secret\" --content backup",
            stdin=secret + "\n",
        )

    def creds(self, hctx) -> dict:
        u = hctx.user
        token = secrets.get(u, KIT, "api.token")
        if token is None:
            raise HitkitError("the pbs kit has not been brought up yet", hint="hitkit up pbs")
        return {
            "url": URL,
            "username": "root@pam",
            "password": secrets.get(u, KIT, "root.password"),
            "api_token": token,
            "api_header": f"Authorization: PBSAPIToken={token}",
            "datastore": f"{DATASTORE} (on the 100 GiB data disk)",
            "repository": f"{ADMIN_TOKEN_ID}@{SERVER}:{DATASTORE}",
            "ssh": "hitkit ssh pbs",
        }
