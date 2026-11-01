"""What the IncusOS kits share (docs/plans/incus-os.md, docs/plans/incus-cluster.md).

IncusOS has no shell, so everything goes through the Incus API, and clients authenticate
with certificates hitkit generates: its own, and one for your browser (the web UI).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from hitkit import incus, secrets
from hitkit.addressing import DOMAIN, address, slot_address
from hitkit.errors import HitkitError
from hitkit.kits import Hooks
from hitkit.machine.spec import mac_address
from hitkit.paths import write_private

SEEDED_CLIENTS = ("hitkit", "hitkit-browser")  # trusted through the seed; never revoked


def client_certificate(cn: str) -> tuple[str, str]:
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


def keypair(user, kit: str, name: str, cn: str) -> tuple[str, str]:
    """``<name>.crt`` and ``<name>.key``, generated once. Returns (cert, key) PEM."""
    cert, key = secrets.get(user, kit, f"{name}.crt"), secrets.get(user, kit, f"{name}.key")
    if cert is None or key is None:
        key, cert = client_certificate(cn)
        secrets.put(user, kit, f"{name}.key", key)
        secrets.put(user, kit, f"{name}.crt", cert)
    return cert, key


def browser_bundle(user, kit: str, nickname: str) -> tuple[str, str]:
    """The browser's certificate as a PKCS#12 file for import, and its password."""
    cert, key = keypair(user, kit, "browser", "hitkit-browser")
    password = secrets.get_or_create(user, kit, "browser.p12.password", secrets.password)
    path = secrets.kit_dir(user, kit) / "browser.p12"
    if not path.exists():
        bundle = pkcs12.serialize_key_and_certificates(
            nickname.encode(), serialization.load_pem_private_key(key.encode(), None),
            x509.load_pem_x509_certificate(cert.encode()), None,
            serialization.BestAvailableEncryption(password.encode()))
        write_private(path, bundle, user)
    return str(path), password


def network_seed(subnet: ipaddress.IPv4Network, name: str, roles: list[str]) -> dict:
    """The static address, route and DNS of slot ``name``: docs/incus/incus-os-in-libvirt.md §2."""
    ip, gateway = slot_address(subnet, name), str(address(subnet, 1))
    return {
        "version": "1",
        "dns": {"hostname": name, "domain": DOMAIN, "nameservers": [gateway], "search_domains": [DOMAIN]},
        "interfaces": [{
            "name": "uplink", "hwaddr": mac_address(subnet, ip.packed[3]),
            "addresses": [f"{ip}/{subnet.prefixlen}"],
            "routes": [{"to": "0.0.0.0/0", "via": gateway}],
            "roles": roles,
        }],
    }


def security_seed(ca_pem: bytes) -> dict:
    return {"version": "1", "custom_ca_certs": [ca_pem.decode()], "encryption_recovery_keys": []}


def store_recovery_keys(user, kit: str, api: incus.IncusClient, log, prefix: str = "recovery", label: str = "") -> None:
    """Store the disk and pool recovery keys, once. Reading them also silences IncusOS's
    warning that they were never retrieved."""
    security = api.get("/os/1.0/system/security")
    keys = security["config"].get("encryption_recovery_keys") or []
    if keys and secrets.get(user, kit, f"{prefix}.encryption-key") is None:
        secrets.put(user, kit, f"{prefix}.encryption-key", keys[0])
        log(f"stored the {label}disk encryption recovery key (hitkit creds {kit})")
    pool_key = (security["state"].get("pool_recovery_keys") or {}).get("local")
    if pool_key and secrets.get(user, kit, f"{prefix}.pool-key") is None:
        secrets.put(user, kit, f"{prefix}.pool-key", pool_key)


class TrustHooks(Hooks):
    """Trust tokens for `incus remote add`, and the browser's login certificate. A kit sets
    these, plus ``api_client(hctx)``; ``up_secret`` exists once the kit has been brought up."""

    kit: str
    remote: str
    host: str  # the DNS name a remote connects to
    bundle_name: str  # the browser certificate's name in a browser's certificate list
    up_secret: str

    @property
    def cli_token(self) -> str:
        return f"{self.remote}-cli"

    def address(self) -> str:
        return f"{self.host}:{incus.PORT}"

    def url(self) -> str:
        return f"https://{self.address()}"

    def require_up(self, hctx) -> str:
        pem = secrets.get(hctx.user, self.kit, self.up_secret)
        if pem is None:
            raise HitkitError(f"the {self.kit} kit has not been brought up yet", hint=f"hitkit up {self.kit}")
        return pem

    def ensure_cli_token(self, hctx, api: incus.IncusClient, log) -> None:
        """Keep one pending token for your incus CLI until it has been used."""
        u = hctx.user
        if secrets.get(u, self.kit, "cli.token-used"):
            return
        if any(c["name"] == self.cli_token for c in api.trusted_clients()):
            secrets.put(u, self.kit, "cli.token-used", "yes")
            secrets.delete(u, self.kit, "cli.token")
            return
        if secrets.get(u, self.kit, "cli.token") and any(t["name"] == self.cli_token for t in api.pending_tokens()):
            return
        api.revoke(self.cli_token)  # a pending one whose token string was lost
        secrets.put(u, self.kit, "cli.token", api.create_token(self.cli_token, self.address())["token"])
        log("minted a trust token for your incus CLI")

    def protected(self, name: str) -> bool:
        """A trust-store entry the kit depends on: never listed, revoked or shadowed."""
        return name in SEEDED_CLIENTS

    def browser_certificate(self, hctx) -> dict:
        self.require_up(hctx)
        bundle, password = browser_bundle(hctx.user, self.kit, self.bundle_name)
        cert, _ = keypair(hctx.user, self.kit, "browser", "hitkit-browser")
        return {"bundle": Path(bundle), "password": password, "nickname": self.bundle_name, "certificate": cert}

    # --- tokens: `hitkit token create|list|revoke <kit>` ---------------------------------

    def token_create(self, hctx, name: str, scopes: list[str] | None) -> dict:
        if self.protected(name):
            raise HitkitError(f"{name!r} is the name of a client hitkit itself uses")
        tok = self.api_client(hctx).create_token(name, self.address())
        return {"name": name, "token": tok["token"], "user": name, "scopes": ["incus client"],
                "expires": tok["expires"], "remote_add": f"incus remote add {self.remote} {tok['token']}"}

    def token_list(self, hctx) -> list[dict]:
        api = self.api_client(hctx)
        pending = [{"name": t["name"], "last_eight": "", "scopes": ["pending token"], "expires": t["expires"]}
                   for t in api.pending_tokens()]
        trusted = [{"name": c["name"], "last_eight": c["fingerprint"][-8:], "scopes": ["trusted client"],
                    "expires": None}
                   for c in api.trusted_clients() if not self.protected(c["name"])]
        return pending + trusted

    def token_revoke(self, hctx, name: str) -> bool:
        if self.protected(name):
            raise HitkitError(f"{name!r} is a client hitkit itself uses; revoking it would lock hitkit out")
        if not self.api_client(hctx).revoke(name):
            raise HitkitError(f"no token or client named {name!r}", hint=f"hitkit token list {self.kit}")
        return True
