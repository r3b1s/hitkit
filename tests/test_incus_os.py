"""The incus-os kit and the Incus client (docs/plans/incus-os.md §5–§7).

API behaviour is tested against a fake Incus server over TLS on localhost, so the real
client (pinning, tokens, operations) runs; the server's responses follow what the spike
saw from Incus 7.5.1 (docs/incus/incus-os-in-libvirt.md §7–§8).
"""

from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12

from hitkit import incus, kits, secrets
from hitkit.addressing import parse_subnet
from hitkit.errors import HitkitError
from hitkit.host.ca import create_ca
from hitkit.machine.lifecycle import HostContext

SUBNET = parse_subnet("10.77.0.0/24")
KIT = "incus-os"


@pytest.fixture
def hctx(user):
    cfg = SimpleNamespace(install_id="test-install", network=lambda: SUBNET)
    return HostContext(user=user, cfg=cfg, ca=create_ca(SUBNET, "test-install"))


@pytest.fixture
def hooks():
    return kits.hooks(KIT)


@pytest.fixture
def spec():
    return kits.load(KIT).machines[0]


def _self_signed(cn: str) -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(x509.NameOID.ORGANIZATION_NAME, "Linux Containers"),
                      x509.NameAttribute(x509.NameOID.COMMON_NAME, cn)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=365)).sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


class FakeIncus:
    """Just enough of Incus's API: /1.0, certificates, token operations, IncusOS security."""

    def __init__(self, tmp_path):
        self.cert_pem, key_pem = _self_signed("root@incus-os")
        (tmp_path / "srv.crt").write_text(self.cert_pem)
        (tmp_path / "srv.key").write_text(key_pem)
        self.auth = "trusted"
        self.certificates = [{"name": "hitkit", "type": "client", "fingerprint": "a" * 64},
                             {"name": "hitkit-browser", "type": "client", "fingerprint": "b" * 64}]
        self.running: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, metadata=None, kind="sync", error=""):
                body = json.dumps({"type": kind, "status_code": status, "error": error,
                                   "metadata": metadata}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                routes = {
                    "/1.0": lambda: {"auth": fake.auth, "environment": {"server": "incus"}},
                    "/1.0/certificates?recursion=1": lambda: fake.certificates,
                    "/1.0/operations?recursion=1": lambda: {"running": fake.running},
                    "/os/1.0/system/security": lambda: {
                        "config": {"encryption_recovery_keys": ["enc-recovery-key-1"]},
                        "state": {"pool_recovery_keys": {"local": "pool-recovery-key-1"}}},
                }
                if self.path in routes:
                    return self._send(200, routes[self.path]())
                self._send(404, error="not found")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.path == "/1.0/certificates" and body["token"] is True
                op = {"id": f"op-{len(fake.running)}-{body['name']}", "class": "token",
                      "metadata": {"fingerprint": incus.fingerprint(fake.cert_pem),
                                   "addresses": ["10.77.0.2:8443", "10.241.190.1:8443"],
                                   "secret": f"secret-for-{body['name']}", "expires_at": None,
                                   "request": {"name": body["name"], "type": "client", "token": True}}}
                fake.running.append(op)
                self._send(202, op, kind="async")

            def do_DELETE(self):
                n = len(fake.running) + len(fake.certificates)
                fake.running = [o for o in fake.running if self.path != f"/1.0/operations/{o['id']}"]
                fake.certificates = [c for c in fake.certificates
                                     if self.path != f"/1.0/certificates/{c['fingerprint']}"]
                self._send(200 if n > len(fake.running) + len(fake.certificates) else 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.handle_error = lambda *a: None  # clients that hang up on purpose (pin checks)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tmp_path / "srv.crt", tmp_path / "srv.key")
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def use_token(self, name: str) -> None:
        """What `incus remote add <token>` does: the token becomes a trusted client."""
        self.running = [o for o in self.running if o["metadata"]["request"]["name"] != name]
        self.certificates.append({"name": name, "type": "client", "fingerprint": "c" * 64})


@pytest.fixture
def fake(tmp_path, hooks, monkeypatch):
    server = FakeIncus(tmp_path)
    monkeypatch.setattr(incus, "PORT", server.port)
    mod = sys.modules[type(hooks).__module__]
    monkeypatch.setattr(mod, "slot_address", lambda subnet, name: ipaddress.ip_address("127.0.0.1"))
    yield server
    server.server.shutdown()
    server.server.server_close()


# --- seed --------------------------------------------------------------------------


def test_seed_matches_what_worked_in_the_spike(hctx, hooks, spec):
    seed = hooks.seed(hctx, spec, (spec,))
    assert set(seed) == {"install", "network", "incus", "security"}  # no kernel section: it breaks startup
    assert seed["install"] == {"version": "1", "force_reboot": False, "target": {"id": "hitkit-data"}}
    net = seed["network"]
    assert net["dns"] == {"hostname": "incus-os", "domain": "hitkit.internal", "nameservers": ["10.77.0.1"],
                          "search_domains": ["hitkit.internal"]}
    assert net["interfaces"] == [{"name": "uplink", "hwaddr": "52:54:00:68:00:02", "addresses": ["10.77.0.2/24"],
                                  "routes": [{"to": "0.0.0.0/0", "via": "10.77.0.1"}],
                                  "roles": ["management", "instances"]}]
    clients = seed["incus"]["preseed"]["certificates"]
    assert [(c["name"], c["type"]) for c in clients] == [("hitkit", "client"), ("hitkit-browser", "client")]
    assert clients[0]["certificate"] == secrets.get(hctx.user, KIT, "client.crt")
    assert seed["security"]["custom_ca_certs"] == [hctx.ca.cert_pem.decode()]


def test_seed_takes_the_prefix_length_from_the_subnet(user, hooks, spec):
    subnet = ipaddress.ip_network("10.77.0.0/23")
    cfg = SimpleNamespace(install_id="x", network=lambda: subnet)
    seed = hooks.seed(HostContext(user=user, cfg=cfg, ca=create_ca(subnet, "x")), spec, (spec,))
    assert seed["network"]["interfaces"][0]["addresses"] == ["10.77.0.2/23"]


def test_a_fresh_install_forgets_the_previous_install(hctx, hooks, spec):
    for name in ("server.crt", "cli.token", "cli.token-used", "recovery.encryption-key", "recovery.pool-key"):
        secrets.put(hctx.user, KIT, name, "from-an-earlier-install")
    hooks.seed(hctx, spec, (spec,))
    client = secrets.get(hctx.user, KIT, "client.crt")
    for name in ("server.crt", "cli.token", "cli.token-used", "recovery.encryption-key"):
        secrets.put(hctx.user, KIT, name, "from-an-earlier-install")
    hooks.seed(hctx, spec, (spec,))
    assert secrets.get(hctx.user, KIT, "server.crt") is None
    assert secrets.get(hctx.user, KIT, "cli.token") is None
    assert secrets.get(hctx.user, KIT, "cli.token-used") is None
    assert secrets.get(hctx.user, KIT, "recovery.encryption-key") is None
    assert secrets.get(hctx.user, KIT, "client.crt") == client  # hitkit's own identity stays


def test_client_certificates_are_generated_once_and_the_browser_bundle_opens(hctx, hooks, spec):
    hooks.seed(hctx, spec, (spec,))
    first = {n: secrets.get(hctx.user, KIT, n) for n in ("client.crt", "browser.crt", "browser.p12.password")}
    hooks.seed(hctx, spec, (spec,))
    assert {n: secrets.get(hctx.user, KIT, n) for n in first} == first
    p12 = (secrets.kit_dir(hctx.user, KIT) / "browser.p12").read_bytes()
    key, cert, _ = pkcs12.load_key_and_certificates(p12, first["browser.p12.password"].encode())
    assert key is not None
    assert cert.public_bytes(serialization.Encoding.PEM).decode() == first["browser.crt"]


# --- client ------------------------------------------------------------------------


def test_token_string_is_what_incus_remote_add_accepts():
    tok = incus.token_string("laptop", "f" * 64, "incus-os.hitkit.internal:8443", "s3cret", None)
    assert json.loads(base64.b64decode(tok)) == {
        "client_name": "laptop", "fingerprint": "f" * 64, "addresses": ["incus-os.hitkit.internal:8443"],
        "secret": "s3cret", "expires_at": None}


def test_client_refuses_a_server_whose_certificate_is_not_the_pinned_one(hctx, hooks, fake):
    other, _ = _self_signed("someone-else")
    with pytest.raises(HitkitError, match="other than the pinned one"):
        hooks.api_client(hctx, other).get("/1.0")
    assert hooks.api_client(hctx, fake.cert_pem).get("/1.0")["auth"] == "trusted"


# --- readiness ---------------------------------------------------------------------


def test_probe_pins_the_server_certificate_on_first_contact(hctx, hooks, spec, fake):
    assert hooks.probe_ready(hctx, spec) is None
    assert incus.fingerprint(secrets.get(hctx.user, KIT, "server.crt")) == incus.fingerprint(fake.cert_pem)
    assert hooks.probe_ready(hctx, spec) is None


def test_probe_reports_why_it_is_not_ready(hctx, hooks, spec, fake):
    fake.auth = "untrusted"
    assert "does not trust" in hooks.probe_ready(hctx, spec)
    secrets.put(hctx.user, KIT, "server.crt", _self_signed("someone-else")[0])
    with pytest.raises(HitkitError, match="pinned"):  # the machine layer reports it and keeps polling
        hooks.probe_ready(hctx, spec)


def test_probe_before_the_server_listens_is_not_an_error(hctx, hooks, spec, fake, monkeypatch):
    monkeypatch.setattr(incus, "PORT", 1)  # nothing listens there
    assert "no TLS answer" in hooks.probe_ready(hctx, spec)
    assert secrets.get(hctx.user, KIT, "server.crt") is None


# --- after_ready, next steps and creds ----------------------------------------------


def test_after_ready_stores_recovery_keys_and_one_cli_token_until_it_is_used(hctx, hooks, spec, fake):
    hooks.probe_ready(hctx, spec)
    log = []
    hooks.after_ready(hctx, spec, (spec,), {}, log.append)
    assert secrets.get(hctx.user, KIT, "recovery.encryption-key") == "enc-recovery-key-1"
    assert secrets.get(hctx.user, KIT, "recovery.pool-key") == "pool-recovery-key-1"
    token = secrets.get(hctx.user, KIT, "cli.token")
    payload = json.loads(base64.b64decode(token))
    assert payload["addresses"] == [f"incus-os.hitkit.internal:{fake.port}"]
    assert payload["fingerprint"] == incus.fingerprint(fake.cert_pem)
    assert f"  incus remote add hitkit-incus-os {token}" in hooks.next_steps(hctx, (spec,))

    hooks.after_ready(hctx, spec, (spec,), {}, log.append)  # idempotent: same token, no new one
    assert secrets.get(hctx.user, KIT, "cli.token") == token
    assert len(fake.running) == 1

    fake.use_token("hitkit-incus-os-cli")
    hooks.after_ready(hctx, spec, (spec,), {}, log.append)
    assert secrets.get(hctx.user, KIT, "cli.token") is None
    assert len(fake.running) == 0
    steps = "\n".join(hooks.next_steps(hctx, (spec,)))
    assert "incus remote add" not in steps and "incus list hitkit-incus-os:" in steps


def test_a_lost_cli_token_is_replaced_and_the_stale_one_cancelled(hctx, hooks, spec, fake):
    hooks.probe_ready(hctx, spec)
    hooks.after_ready(hctx, spec, (spec,), {}, lambda m: None)
    secrets.delete(hctx.user, KIT, "cli.token")
    hooks.after_ready(hctx, spec, (spec,), {}, lambda m: None)
    assert secrets.get(hctx.user, KIT, "cli.token")
    assert len(fake.running) == 1


def test_creds(hctx, hooks, spec, fake):
    with pytest.raises(HitkitError, match="not been brought up"):
        hooks.creds(hctx)
    hooks.probe_ready(hctx, spec)
    hooks.after_ready(hctx, spec, (spec,), {}, lambda m: None)
    data = hooks.creds(hctx)
    assert data["url"] == "https://incus-os.hitkit.internal:8443".replace("8443", str(fake.port))
    assert data["ui"] == data["url"] + "/ui/"
    assert data["server_fingerprint"] == incus.fingerprint(fake.cert_pem)
    assert data["remote_add"] == f"incus remote add hitkit-incus-os {secrets.get(hctx.user, KIT, 'cli.token')}"
    assert data["ui_certificate"].endswith("browser.p12")
    assert data["ui_certificate_password"] == secrets.get(hctx.user, KIT, "browser.p12.password")
    assert data["encryption_recovery_key"] == "enc-recovery-key-1"
    assert data["pool_recovery_key"] == "pool-recovery-key-1"


# --- tokens ------------------------------------------------------------------------


def test_tokens_create_list_and_revoke(hctx, hooks, spec, fake):
    hooks.probe_ready(hctx, spec)
    tok = hooks.token_create(hctx, "laptop", None)
    assert tok["remote_add"] == f"incus remote add hitkit-incus-os {tok['token']}"
    hooks.token_create(hctx, "tablet", None)
    fake.use_token("tablet")
    listed = {t["name"]: t["scopes"] for t in hooks.token_list(hctx)}
    assert listed == {"laptop": ["pending token"], "tablet": ["trusted client"]}  # not hitkit's own clients

    assert hooks.token_revoke(hctx, "laptop") is True
    assert hooks.token_revoke(hctx, "tablet") is True
    assert hooks.token_list(hctx) == []
    with pytest.raises(HitkitError, match="no token or client named 'laptop'"):
        hooks.token_revoke(hctx, "laptop")


def test_hitkits_own_clients_cannot_be_revoked_or_shadowed(hctx, hooks, spec, fake):
    hooks.probe_ready(hctx, spec)
    for name in ("hitkit", "hitkit-browser"):
        with pytest.raises(HitkitError, match="lock hitkit out"):
            hooks.token_revoke(hctx, name)
        with pytest.raises(HitkitError, match="hitkit itself uses"):
            hooks.token_create(hctx, name, None)
    assert len(fake.certificates) == 2


# --- cli ---------------------------------------------------------------------------


def test_ssh_to_an_installer_machine_explains_there_is_no_shell():
    from hitkit import cli

    result = CliRunner().invoke(cli.cli, ["ssh", "incus-os"])
    assert result.exit_code != 0
    assert "has no shell" in result.output
    assert "hitkit creds incus-os" in result.output
