"""Wiring for the netbird kit.

The image runs NetBird's combined server and dashboard behind Traefik, as the
upstream quickstart does, with TLS from the hitkit CA. Its store, relay and
cookie keys are host secrets delivered as credentials. Once it is ready, hitkit
claims the fresh instance through the setup API: the owner account (the human's
dashboard login) and an owner API token that hitkit keeps and renews before it
expires. It also keeps a reusable setup key for enrolling test peers.
`hitkit token` creates a service user per token, so agents get their own
revocable identity with a chosen role.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from hitkit import secrets, trace
from hitkit.errors import HitkitError
from hitkit.host.ca import ca_cert_path
from hitkit.kits import Hooks

_t = trace.logger(__name__)

KIT = "netbird"
URL = "https://netbird.hitkit.internal"
TOKEN_NAME = "hitkit"
TOKEN_DAYS = 365  # NetBird's maximum token lifetime
RENEW_DAYS = 60  # `up` and `token` renew hitkit's token once it has less than this left
SETUP_KEY_NAME = "hitkit"
ROLES = ("admin", "user", "auditor", "network_admin", "billing_admin")


class NetBirdError(HitkitError):
    def __init__(self, status: int, message: str):
        super().__init__(f"NetBird API error {status}: {message}")
        self.status = status


class NetBird:
    """Just the REST calls hitkit makes, verified against the hitkit CA."""

    def __init__(self, cafile: Path, token: str | None = None):
        self.cafile = cafile
        self.token = token

    def request(self, method: str, path: str, body=None, ok=(200, 201, 204)):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{URL}/api{path}", data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Token {self.token}")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(cafile=str(self.cafile)),
                                        timeout=30) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except urllib.error.URLError as e:
            raise HitkitError(f"cannot reach NetBird at {URL}: {e.reason}",
                              hint="new kits need their DNS name: sudo .venv/bin/hitkit setup") from None
        _t.debug("netbird api", method=method, path=path, status=status, auth=bool(self.token),
                 duration_ms=round((time.monotonic() - started) * 1000))
        if status not in ok:
            try:
                message = json.loads(raw).get("message", raw.decode(errors="replace"))
            except (ValueError, AttributeError):
                message = raw.decode(errors="replace")
            raise NetBirdError(status, message)
        result = json.loads(raw) if raw.strip() else None
        if isinstance(result, dict):  # plain tokens and setup keys are only ever shown once
            for key in ("personal_access_token", "plain_token", "key"):
                if isinstance(result.get(key), str):
                    trace.secret(result[key])
        return result

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body=None):
        return self.request("POST", path, body)

    def delete(self, path: str):
        return self.request("DELETE", path, ok=(200, 204, 404))


def _b64_key() -> str:
    """32 random bytes, padded standard base64 (what NetBird's store and cookie keys need)."""
    return base64.b64encode(os.urandom(32)).decode()


def owner_email(hctx) -> str:
    name = "".join(c for c in hctx.user.name.lower() if c.isalnum() or c in "-_.")
    return f"{name or 'owner'}@hitkit.internal"


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


class KitHooks(Hooks):
    def credentials(self, hctx, machine, plan):
        u = hctx.user
        return {
            "hitkit.nb.store-key": secrets.get_or_create(u, KIT, "store.key", _b64_key).encode(),
            "hitkit.nb.cookie-key": secrets.get_or_create(u, KIT, "cookie.key", _b64_key).encode(),
            "hitkit.nb.relay-secret": secrets.get_or_create(u, KIT, "relay.secret", secrets.password).encode(),
        }

    def after_ready(self, hctx, machine, plan, knobs, log):
        api = self.admin_api(hctx, log)
        keys = api.get("/setup-keys")
        if secrets.get(hctx.user, KIT, "setup-key") and any(
                k["name"] == SETUP_KEY_NAME and k.get("valid") and not k.get("revoked") for k in keys):
            return
        log(f"creating reusable setup key {SETUP_KEY_NAME!r} for peers")
        key = api.post("/setup-keys", {"name": SETUP_KEY_NAME, "type": "reusable", "expires_in": 0,
                                       "auto_groups": [], "usage_limit": 0, "ephemeral": False})["key"]
        secrets.put(hctx.user, KIT, "setup-key", key)

    # --- hitkit's own API token -----------------------------------------------

    def admin_api(self, hctx, log=lambda msg: None) -> NetBird:
        """An API client with hitkit's owner token: claims a fresh instance, renews an expiring token."""
        u = hctx.user
        cafile = ca_cert_path(u)
        # The previous token too: a restored backup may predate the last renewal.
        for name in ("admin.token", "admin.token.previous"):
            token = secrets.get(u, KIT, name)
            if token and self._valid(NetBird(cafile, token)):
                api = NetBird(cafile, token)
                self._renew(hctx, api, log, force=name != "admin.token")
                return api
        if not NetBird(cafile).get("/instance")["setup_required"]:
            raise HitkitError(
                "NetBird is set up, but hitkit holds no valid API token for it (tokens last "
                f"{TOKEN_DAYS} days and `hitkit up netbird` renews them)",
                hint="create a token in the dashboard as the owner (hitkit creds netbird), or start "
                     "over, deleting NetBird's data: hitkit destroy netbird --delete-data --yes")
        return self._claim(hctx, cafile, log)

    @staticmethod
    def _valid(api: NetBird) -> bool:
        try:
            api.get("/users/current")
            return True
        except NetBirdError as e:
            if e.status in (401, 403):
                return False
            raise

    def _claim(self, hctx, cafile: Path, log) -> NetBird:
        u = hctx.user
        email = owner_email(hctx)
        password = secrets.get_or_create(u, KIT, "owner.password", secrets.password)
        log(f"claiming the new NetBird instance: owner {email}")
        reply = NetBird(cafile).post("/setup", {"email": email, "name": u.name, "password": password,
                                                "create_pat": True, "pat_expire_in": TOKEN_DAYS})
        token = reply.get("personal_access_token")
        if not token:
            raise HitkitError("NetBird created the owner but returned no API token",
                              hint="NB_SETUP_PAT_ENABLED must be set on netbird-server (images/netbird)")
        secrets.put(u, KIT, "admin.token", token)
        secrets.put(u, KIT, "owner.email", email)
        self._save_expiry(hctx)
        secrets.put(u, KIT, "setup-key", "")  # any kept key belonged to an earlier instance
        return NetBird(cafile, token)

    def _save_expiry(self, hctx) -> None:
        expires = _today() + dt.timedelta(days=TOKEN_DAYS)
        secrets.put(hctx.user, KIT, "admin.token.json", json.dumps({"expires": expires.isoformat()}))

    def _expires(self, hctx) -> dt.date | None:
        raw = secrets.get(hctx.user, KIT, "admin.token.json")
        return dt.date.fromisoformat(json.loads(raw)["expires"]) if raw else None

    def _renew(self, hctx, api: NetBird, log, force: bool = False) -> None:
        expires = self._expires(hctx)
        if not force and expires and (expires - _today()).days >= RENEW_DAYS:
            return
        log("renewing hitkit's NetBird API token")
        me = api.get("/users/current")["id"]
        new = api.post(f"/users/{me}/tokens", {"name": TOKEN_NAME, "expires_in": TOKEN_DAYS})
        u = hctx.user
        secrets.put(u, KIT, "admin.token.previous", api.token)
        secrets.put(u, KIT, "admin.token", new["plain_token"])
        self._save_expiry(hctx)
        api.token = new["plain_token"]
        keep = new["personal_access_token"]["id"]
        for t in api.get(f"/users/{me}/tokens"):  # the setup token and older renewals
            if t["name"] in (TOKEN_NAME, "setup-token") and t["id"] != keep:
                api.delete(f"/users/{me}/tokens/{t['id']}")

    # --- user-facing ------------------------------------------------------------

    def creds(self, hctx) -> dict:
        u = hctx.user
        token = secrets.get(u, KIT, "admin.token")
        if token is None:
            raise HitkitError("the netbird kit has not been brought up yet", hint="hitkit up netbird")
        expires = self._expires(hctx)
        return {
            "url": URL,
            "email": secrets.get(u, KIT, "owner.email"),
            "password": secrets.get(u, KIT, "owner.password"),
            "api_url": f"{URL}/api",
            "api_token": token,
            "api_header": f"Authorization: Token {token}",
            "api_token_expires": f"{expires} (renewed by `hitkit up netbird`)" if expires else None,
            "setup_key": secrets.get(u, KIT, "setup-key") or None,
            "peer_join": f"netbird up --management-url {URL} --setup-key <setup_key>",
            "ca_certificate": str(ca_cert_path(u)),
            "ssh": "hitkit ssh netbird",
        }

    def _service_user(self, api: NetBird, name: str) -> dict | None:
        return next((s for s in api.get("/users?service_user=true") if s["name"] == name), None)

    def token_create(self, hctx, name: str, scopes: list[str] | None) -> dict:
        """A service user named NAME with the role given as its only scope (default admin)."""
        role = (scopes or ["admin"])[0]
        if len(scopes or []) > 1 or role not in ROLES:
            raise HitkitError("a NetBird token takes one --scope: its service user's role",
                              hint=f"roles: {', '.join(ROLES)}")
        api = self.admin_api(hctx)
        if self._service_user(api, name):
            raise HitkitError(f"NetBird service user {name!r} already exists",
                              hint=f"hitkit token revoke netbird {name}")
        user = api.post("/users", {"name": name, "role": role, "auto_groups": [], "is_service_user": True})
        tok = api.post(f"/users/{user['id']}/tokens", {"name": name, "expires_in": TOKEN_DAYS})
        return {"name": name, "token": tok["plain_token"], "scopes": [role], "user": name,
                "expires": tok["personal_access_token"]["expiration_date"]}

    def token_list(self, hctx) -> list[dict]:
        api = self.admin_api(hctx)
        out = []
        for s in api.get("/users?service_user=true"):
            tokens = api.get(f"/users/{s['id']}/tokens")
            out.append({"name": s["name"], "scopes": [s["role"]], "last_eight": None,
                        "expires": min((t["expiration_date"] for t in tokens), default=None)})
        return out

    def token_revoke(self, hctx, name: str) -> bool:
        """Deletes the service user, and with it its tokens."""
        api = self.admin_api(hctx)
        user = self._service_user(api, name)
        if user is None:
            raise HitkitError(f"no NetBird service user {name!r}", hint="hitkit token list netbird")
        api.delete(f"/users/{user['id']}")
        return True
