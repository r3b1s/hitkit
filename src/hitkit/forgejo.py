"""Minimal Forgejo REST client: only the calls hitkit makes. Standard library only.

TLS is verified against the hitkit CA, never disabled.
"""

from __future__ import annotations

import base64
import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from hitkit import trace
from hitkit.errors import HitkitError

_t = trace.logger(__name__)

FORGE_URL = "https://forgejo.hitkit.internal"


class ForgejoError(HitkitError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Forgejo API error {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class Auth:
    token: str | None = None
    basic: tuple[str, str] | None = None

    @property
    def kind(self) -> str:
        return "token" if self.token else "basic" if self.basic else "none"

    def header(self) -> str | None:
        if self.token:
            return f"token {self.token}"
        if self.basic:
            raw = f"{self.basic[0]}:{self.basic[1]}".encode()
            return "Basic " + base64.b64encode(raw).decode()
        return None


class Forgejo:
    def __init__(self, cafile: Path, auth: Auth = Auth(), base_url: str = FORGE_URL):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.auth = auth
        self._ctx = ssl.create_default_context(cafile=str(cafile))

    def with_auth(self, auth: Auth) -> "Forgejo":
        clone = Forgejo.__new__(Forgejo)
        clone.base, clone.auth, clone._ctx = self.base, auth, self._ctx
        return clone

    def request(self, method: str, path: str, body=None, ok=(200, 201, 204)):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if header := self.auth.header():
            req.add_header("Authorization", header)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except urllib.error.URLError as e:
            _t.warning("forgejo unreachable", method=method, path=path, error=str(e.reason))
            raise HitkitError(f"cannot reach Forgejo at {self.base}: {e.reason}") from None
        fields = {"method": method, "path": path, "status": status, "auth": self.auth.kind,
                  "duration_ms": round((time.monotonic() - started) * 1000)}
        if status in ok:
            _t.debug("forgejo api", **fields)
        else:
            _t.info("forgejo api error", body=raw[:1000].decode(errors="replace"), **fields)
        if status not in ok:
            try:
                message = json.loads(raw).get("message", raw.decode(errors="replace"))
            except (ValueError, AttributeError):
                message = raw.decode(errors="replace")
            raise ForgejoError(status, message)
        data = json.loads(raw) if raw.strip() else None
        if isinstance(data, dict):  # minted access tokens and runner registration tokens
            trace.secret(data.get("sha1"))
            trace.secret(data.get("token"))
        return data

    def get(self, path: str):
        return self.request("GET", path)

    def exists(self, path: str) -> bool:
        try:
            self.request("GET", path)
            return True
        except ForgejoError as e:
            if e.status == 404:
                return False
            raise

    def post(self, path: str, body=None):
        return self.request("POST", path, body)

    def put(self, path: str, body=None):
        return self.request("PUT", path, body)

    def patch(self, path: str, body=None):
        return self.request("PATCH", path, body)

    def delete(self, path: str, ok=(204, 404)):
        return self.request("DELETE", path, ok=ok)
