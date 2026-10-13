"""Wiring for the ludus kit.

The image installs Ludus on first boot (images/ludus). Over SSH, hitkit then
creates the human's Ludus admin through the admin API (localhost-only, port
8081, authenticated with the root API key the installer wrote) and keeps that
admin's API key, so `hitkit creds ludus` can hand out a ready LUDUS_API_KEY.
"""

from __future__ import annotations

import json
import re

from hitkit import secrets
from hitkit.errors import HitkitError
from hitkit.kits import Hooks
from hitkit.machine import remote

KIT = "ludus"
ADMIN_API = "https://{fqdn}:8081/api/v2"
_ADMIN_CURL = (
    "curl -sS --cacert /etc/hitkit/ca.crt --resolve {fqdn}:8081:127.0.0.1 "
    "-H \"X-API-KEY: $(cat /opt/ludus/install/root-api-key)\" -H 'Content-Type: application/json' "
)


def _fqdn(machine: str) -> str:
    return f"{machine}.hitkit.internal"


def user_id(username: str) -> str:
    """A Ludus user ID (^[A-Za-z0-9]{1,20}$) from the host account name."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", username)[:20]
    return cleaned if cleaned and cleaned.lower() not in ("root", "admin", "ludus") else "hitkit"


class KitHooks(Hooks):
    def credentials(self, hctx, machine, plan):
        pw = secrets.get_or_create(hctx.user, KIT, "root.password", secrets.password)
        return {"hitkit.pve.root-password": pw.encode()}  # the name the Proxmox image applies

    def after_ready(self, hctx, machine, plan, knobs, log):
        if secrets.get(hctx.user, KIT, "api.key"):
            return
        fqdn = _fqdn(machine.name)
        uid = user_id(hctx.user.name)
        password = secrets.get_or_create(hctx.user, KIT, "user.password", secrets.password)
        body = json.dumps({"name": hctx.user.name, "userID": uid, "email": f"{uid}@hitkit.internal",
                           "password": password, "isAdmin": True})
        log(f"creating Ludus admin {uid!r}")
        out = remote.run(hctx, machine.name,
                         _ADMIN_CURL.format(fqdn=fqdn) + f"--data-binary @- {ADMIN_API.format(fqdn=fqdn)}/user",
                         stdin=body)
        reply = _json(out)
        key = reply.get("apiKey") or (reply.get("result") or {}).get("apiKey")
        if not key and "already exists" in out:
            # Created on an earlier run whose key was lost: issue a new one for that user.
            log(f"Ludus admin {uid!r} exists; issuing a new API key")
            out = remote.run(hctx, machine.name, _ADMIN_CURL.format(fqdn=fqdn)
                             + f"'{ADMIN_API.format(fqdn=fqdn)}/user/apikey?userID={uid}'")
            key = (_json(out).get("result") or {}).get("apiKey")
        if not key:
            raise HitkitError(f"could not create the Ludus admin: {out.strip()[:300]}",
                              hint=f"hitkit ssh {machine.name} -- journalctl -u ludus-admin -b")
        secrets.put(hctx.user, KIT, "api.key", key)
        secrets.put(hctx.user, KIT, "user.id", uid)

    def creds(self, hctx) -> dict:
        key = secrets.get(hctx.user, KIT, "api.key")
        if key is None:
            raise HitkitError("ludus has not been brought up", hint="hitkit up ludus")
        fqdn = _fqdn("ludus")
        url = f"https://{fqdn}:8080"
        return {
            "url": url,
            "user_id": secrets.get(hctx.user, KIT, "user.id"),
            "api_key": key,
            "password": secrets.get(hctx.user, KIT, "user.password"),
            "client_env": f"LUDUS_URL={url} LUDUS_API_KEY=<api_key>",
            "client_in_vm": "hitkit ssh ludus -- ludus --help   (the client is installed there)",
            "wireguard": "ludus user wireguard > ludus.conf   (then wg-quick up ./ludus.conf)",
            "proxmox_ui": f"https://{fqdn}:8006 (root@pam, password: proxmox_root_password)",
            "proxmox_root_password": secrets.get(hctx.user, KIT, "root.password"),
            "ca_certificate": str(hctx.user.ca_dir / "ca.crt"),
        }


def _json(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
