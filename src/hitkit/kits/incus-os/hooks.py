"""Wiring for the incus-os kit (docs/plans/incus-os.md).

IncusOS is installed from its vendor's image and has no shell, so everything goes
through the Incus API. The install seed makes the server trust two client certificates
hitkit generates: its own, and one for your browser (the web UI). hitkit pins the
server's self-signed certificate on first contact (design §5), stores the disk recovery
keys, and hands out trust tokens for `incus remote add`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from hitkit import incus, secrets, trace
from hitkit.addressing import DOMAIN, address, slot, slot_address
from hitkit.errors import HitkitError
from hitkit.kits import Hooks
from hitkit.machine.spec import mac_address
from hitkit.paths import write_private

KIT = MACHINE = "incus-os"
REMOTE = "hitkit-incus-os"
CLI_TOKEN = "hitkit-incus-os-cli"
BUNDLE_NAME = "hitkit incus-os"  # the browser certificate's name in a browser's certificate list
SEEDED_CLIENTS = ("hitkit", "hitkit-browser")  # trusted through the seed; never revoked
# Secrets that belong to one install of the server: a fresh install starts without them.
PER_INSTALL = ("server.crt", "cli.token", "cli.token-used", "recovery.encryption-key", "recovery.pool-key")

_t = trace.logger(__name__)


def _address() -> str:
    return f"{slot(MACHINE).fqdn()}:{incus.PORT}"


def _url() -> str:
    return f"https://{_address()}"


def _client_certificate(cn: str) -> tuple[str, str]:
    """A self-signed TLS client certificate, as Incus clients use. Returns (key, cert) PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


def _keypair(user, name: str, cn: str) -> tuple[str, str]:
    """``<name>.crt`` and ``<name>.key``, generated once. Returns (cert, key) PEM."""
    cert, key = secrets.get(user, KIT, f"{name}.crt"), secrets.get(user, KIT, f"{name}.key")
    if cert is None or key is None:
        key, cert = _client_certificate(cn)
        secrets.put(user, KIT, f"{name}.key", key)
        secrets.put(user, KIT, f"{name}.crt", cert)
    return cert, key


def _browser_bundle(user) -> tuple[str, str]:
    """The browser's certificate as a PKCS#12 file for import, and its password."""
    cert, key = _keypair(user, "browser", "hitkit-browser")
    password = secrets.get_or_create(user, KIT, "browser.p12.password", secrets.password)
    path = secrets.kit_dir(user, KIT) / "browser.p12"
    if not path.exists():
        bundle = pkcs12.serialize_key_and_certificates(
            BUNDLE_NAME.encode(), serialization.load_pem_private_key(key.encode(), None),
            x509.load_pem_x509_certificate(cert.encode()), None,
            serialization.BestAvailableEncryption(password.encode()))
        write_private(path, bundle, user)
    return str(path), password


class KitHooks(Hooks):
    def seed(self, hctx, machine, plan):
        u = hctx.user
        for name in PER_INSTALL:  # an earlier install's pin, tokens and keys are void now
            secrets.delete(u, KIT, name)
        client_cert, _ = _keypair(u, "client", "hitkit")
        browser_cert, _ = _keypair(u, "browser", "hitkit-browser")
        _browser_bundle(u)
        subnet = hctx.cfg.network()
        ip, gateway = slot_address(subnet, machine.name), str(address(subnet, 1))
        # The files that worked in the spike: docs/incus/incus-os-in-libvirt.md §2.
        return {
            "install": {"version": "1", "force_reboot": False, "target": {"id": "hitkit-data"}},
            "network": {
                "version": "1",
                "dns": {"hostname": machine.name, "domain": DOMAIN, "nameservers": [gateway],
                        "search_domains": [DOMAIN]},
                "interfaces": [{
                    "name": "uplink", "hwaddr": mac_address(subnet, ip.packed[3]),
                    "addresses": [f"{ip}/{subnet.prefixlen}"],
                    "routes": [{"to": "0.0.0.0/0", "via": gateway}],
                    "roles": ["management", "instances"],
                }],
            },
            "incus": {
                "version": "1", "apply_defaults": True,
                "preseed": {"certificates": [
                    {"name": "hitkit", "type": "client", "certificate": client_cert},
                    {"name": "hitkit-browser", "type": "client", "certificate": browser_cert},
                ]},
            },
            "security": {"version": "1", "custom_ca_certs": [hctx.ca.cert_pem.decode()],
                         "encryption_recovery_keys": []},
        }

    def api_client(self, hctx, server_pem: str | None = None) -> incus.IncusClient:
        pem = server_pem or secrets.get(hctx.user, KIT, "server.crt")
        if pem is None:
            raise HitkitError("the incus-os kit has not been brought up yet", hint="hitkit up incus-os")
        _keypair(hctx.user, "client", "hitkit")
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
        u = hctx.user
        api = self.api_client(hctx)
        security = api.get("/os/1.0/system/security")
        keys = security["config"].get("encryption_recovery_keys") or []
        if keys and secrets.get(u, KIT, "recovery.encryption-key") is None:
            secrets.put(u, KIT, "recovery.encryption-key", keys[0])
            log("stored the disk encryption recovery key (hitkit creds incus-os)")
        pool_key = (security["state"].get("pool_recovery_keys") or {}).get("local")
        if pool_key and secrets.get(u, KIT, "recovery.pool-key") is None:
            secrets.put(u, KIT, "recovery.pool-key", pool_key)
        self._ensure_cli_token(hctx, api, log)

    def _ensure_cli_token(self, hctx, api: incus.IncusClient, log) -> None:
        """Keep one pending token for your incus CLI until it has been used."""
        u = hctx.user
        if secrets.get(u, KIT, "cli.token-used"):
            return
        if any(c["name"] == CLI_TOKEN for c in api.trusted_clients()):
            secrets.put(u, KIT, "cli.token-used", "yes")
            secrets.delete(u, KIT, "cli.token")
            return
        if secrets.get(u, KIT, "cli.token") and any(t["name"] == CLI_TOKEN for t in api.pending_tokens()):
            return
        api.revoke(CLI_TOKEN)  # a pending one whose token string was lost
        secrets.put(u, KIT, "cli.token", api.create_token(CLI_TOKEN, _address())["token"])
        log("minted a trust token for your incus CLI")

    def next_steps(self, hctx, plan):
        token = secrets.get(hctx.user, KIT, "cli.token")
        if token:
            lines = ["Add it to your incus CLI:",
                     f"  incus remote add {REMOTE} {token}",
                     f"  incus launch images:debian/trixie {REMOTE}:test"]
        else:
            lines = [f"Your incus CLI already has it (the token was used): incus list {REMOTE}:"]
        return lines + [f"Web UI: {_url()}  (log in with your certificate: hitkit browser-cert incus-os)",
                        "More tokens: hitkit token create incus-os --name <name>"]

    def creds(self, hctx) -> dict:
        u = hctx.user
        pem = secrets.get(u, KIT, "server.crt")
        if pem is None:
            raise HitkitError("the incus-os kit has not been brought up yet", hint="hitkit up incus-os")
        bundle, password = _browser_bundle(u)
        data = {"url": _url(), "ui": f"{_url()}/ui/", "server_fingerprint": incus.fingerprint(pem)}
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

    def browser_certificate(self, hctx) -> dict:
        if secrets.get(hctx.user, KIT, "server.crt") is None:
            raise HitkitError("the incus-os kit has not been brought up yet", hint="hitkit up incus-os")
        bundle, password = _browser_bundle(hctx.user)
        cert, _ = _keypair(hctx.user, "browser", "hitkit-browser")
        return {"bundle": Path(bundle), "password": password, "nickname": BUNDLE_NAME, "certificate": cert}

    # --- tokens: `hitkit token create|list|revoke incus-os` ------------------------------

    def token_create(self, hctx, name: str, scopes: list[str] | None) -> dict:
        if name in SEEDED_CLIENTS:
            raise HitkitError(f"{name!r} is the name of a client hitkit itself uses")
        tok = self.api_client(hctx).create_token(name, _address())
        return {"name": name, "token": tok["token"], "user": name, "scopes": ["incus client"],
                "expires": tok["expires"], "remote_add": f"incus remote add {REMOTE} {tok['token']}"}

    def token_list(self, hctx) -> list[dict]:
        api = self.api_client(hctx)
        pending = [{"name": t["name"], "last_eight": "", "scopes": ["pending token"], "expires": t["expires"]}
                   for t in api.pending_tokens()]
        trusted = [{"name": c["name"], "last_eight": c["fingerprint"][-8:], "scopes": ["trusted client"],
                    "expires": None}
                   for c in api.trusted_clients() if c["name"] not in SEEDED_CLIENTS]
        return pending + trusted

    def token_revoke(self, hctx, name: str) -> bool:
        if name in SEEDED_CLIENTS:
            raise HitkitError(f"{name!r} is a client hitkit itself uses; revoking it would lock hitkit out")
        if not self.api_client(hctx).revoke(name):
            raise HitkitError(f"no token or client named {name!r}", hint="hitkit token list incus-os")
        return True
