"""Minimal Incus REST client: only the calls hitkit makes (docs/plans/incus-os.md §5.4).

Mutual TLS: hitkit presents its client certificate and checks the server's certificate
against the SHA-256 fingerprint pinned on first contact. A standalone Incus server's
certificate is self-signed and cannot be replaced (docs/incus/clustered-vs-standalone.md),
so there is no CA to verify against; Incus's own client pins the same way.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import ssl
import time
from pathlib import Path

from hitkit import trace
from hitkit.errors import HitkitError

_t = trace.logger(__name__)

PORT = 8443


class IncusError(HitkitError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Incus API error {status}: {message}")
        self.status = status


class PinMismatch(HitkitError):
    pass


def fingerprint(cert_pem: str) -> str:
    """SHA-256 of a certificate's DER form, lowercase hex: what Incus calls its fingerprint."""
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert_pem)).hexdigest()


def server_certificate(host: str, port: int = PORT, timeout: float = 5.0) -> str:
    """The server's certificate as PEM, unverified: for pinning on first contact only."""
    return ssl.get_server_certificate((host, port), timeout=timeout)


def token_string(name: str, fingerprint_: str, address: str, secret: str, expires_at) -> str:
    """The trust token `incus remote add <remote> <token>` accepts (docs/incus/incus-os-in-libvirt.md
    §8). Its one address becomes the remote's URL; the client checks the fingerprint."""
    payload = {"client_name": name, "fingerprint": fingerprint_, "addresses": [address],
               "secret": secret, "expires_at": expires_at}
    return base64.b64encode(json.dumps(payload).encode()).decode()


class IncusClient:
    def __init__(self, host: str, certfile: Path, keyfile: Path, pin: str, port: int = PORT,
                 timeout: float = 30.0):
        self.host, self.port, self.pin, self.timeout = host, port, pin, timeout
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # Python's default too; stated, not assumed
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE  # verified by the pin, in _connect
        self._ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))

    def _connect(self, timeout: float | None = None) -> http.client.HTTPSConnection:
        conn = http.client.HTTPSConnection(self.host, self.port, context=self._ctx,
                                           timeout=timeout or self.timeout, blocksize=1 << 20)
        conn.connect()
        der = conn.sock.getpeercert(binary_form=True)
        if hashlib.sha256(der).hexdigest() != self.pin:
            conn.close()
            _t.error("incus certificate mismatch", host=self.host, pinned=self.pin)
            raise PinMismatch(f"the Incus server at {self.host} presented a certificate other than the pinned one",
                              hint="if the server was reinstalled, `hitkit up` pins its new certificate")
        return conn

    def _exchange(self, method: str, path: str, body=None, headers: dict | None = None,
                  timeout: float | None = None) -> tuple[int, bytes]:
        started = time.monotonic()
        try:
            conn = self._connect(timeout)
            try:
                conn.request(method, path, body=body, headers=headers or {})
                resp = conn.getresponse()
                status, raw = resp.status, resp.read()
            finally:
                conn.close()
        except OSError as e:  # refused, timed out, TLS failure
            _t.info("incus unreachable", method=method, path=path, error=str(e))
            raise HitkitError(f"cannot reach Incus at {self.host}:{self.port}: {e}") from None
        _t.debug("incus api", method=method, path=path, status=status,
                 duration_ms=round((time.monotonic() - started) * 1000))
        return status, raw

    @staticmethod
    def _metadata(status: int, raw: bytes):
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            data = {}
        if status >= 400:
            raise IncusError(status, data.get("error") or raw[:200].decode(errors="replace"))
        return data.get("metadata")

    def request(self, method: str, path: str, body=None, timeout: float | None = None):
        """The response's `metadata` (for an async call: the operation)."""
        status, raw = self._exchange(method, path, json.dumps(body) if body is not None else None,
                                     {"Content-Type": "application/json"} if body is not None else {}, timeout)
        return self._metadata(status, raw)

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body=None):
        return self.request("POST", path, body)

    def put(self, path: str, body=None):
        return self.request("PUT", path, body)

    def patch(self, path: str, body=None):
        return self.request("PATCH", path, body)

    def delete(self, path: str):
        return self.request("DELETE", path)

    def wait(self, operation: dict, timeout: int = 600) -> dict:
        """Block until an async operation ends; a failed one raises."""
        done = self.request("GET", f"/1.0/operations/{operation['id']}/wait?timeout={timeout}", timeout=timeout + 30)
        if done.get("status") != "Success":
            raise HitkitError(f"Incus operation failed: {done.get('err') or done.get('status')}")
        return done

    # --- storage and instances --------------------------------------------------------

    def upload_iso(self, pool: str, name: str, path: Path, timeout: float = 1800) -> None:
        """`incus storage volume import <pool> <path> <name> --type=iso`."""
        with path.open("rb") as f:
            status, raw = self._exchange(
                "POST", f"/1.0/storage-pools/{pool}/volumes/custom", f,
                {"Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size),
                 "X-Incus-name": name, "X-Incus-type": "iso"}, timeout)
        self.wait(self._metadata(status, raw))

    def instance(self, name: str) -> dict | None:
        try:
            return self.get(f"/1.0/instances/{name}")
        except IncusError as e:
            if e.status == 404:
                return None
            raise

    def set_state(self, name: str, action: str, force: bool = False) -> None:
        self.wait(self.put(f"/1.0/instances/{name}/state", {"action": action, "timeout": 60, "force": force}))

    def console_log(self, name: str) -> str:
        """A running VM's serial console so far (not an API response: the bare text)."""
        status, raw = self._exchange("GET", f"/1.0/instances/{name}/console?type=log")
        if status >= 400:
            self._metadata(status, raw)
        return raw.decode(errors="replace")

    # --- trust tokens and trusted clients ---------------------------------------------

    def create_token(self, name: str, address: str) -> dict:
        """Mint a single-use trust token for a new client named ``name``."""
        op = self.post("/1.0/certificates", {"name": name, "type": "client", "token": True})
        meta = op["metadata"]
        token = token_string(name, meta["fingerprint"], address, meta["secret"], meta.get("expires_at"))
        trace.secret(meta["secret"])
        trace.secret(token)
        return {"name": name, "token": token, "fingerprint": meta["fingerprint"], "expires": meta.get("expires_at")}

    def pending_tokens(self) -> list[dict]:
        """Tokens not used yet: the running operations of class `token`."""
        ops = self.get("/1.0/operations?recursion=1") or {}
        return [{"name": op["metadata"]["request"]["name"], "operation": op["id"],
                 "expires": op["metadata"].get("expires_at")}
                for op in ops.get("running", []) if op.get("class") == "token"]

    def trusted_clients(self) -> list[dict]:
        return [{"name": c["name"], "fingerprint": c["fingerprint"], "type": c["type"]}
                for c in self.get("/1.0/certificates?recursion=1") or []]

    def revoke(self, name: str) -> bool:
        """Cancel the pending tokens and remove the trusted clients called ``name``."""
        found = False
        for tok in self.pending_tokens():
            if tok["name"] == name:
                self.delete(f"/1.0/operations/{tok['operation']}")
                found = True
        for c in self.trusted_clients():
            if c["name"] == name:
                self.delete(f"/1.0/certificates/{c['fingerprint']}")
                found = True
        return found
