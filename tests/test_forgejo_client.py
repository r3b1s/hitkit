"""Forgejo REST client: auth headers, request/response handling, error mapping."""

from __future__ import annotations

import base64
import io
import ipaddress
import ssl
import urllib.error

import pytest

from hitkit import forgejo
from hitkit.errors import HitkitError
from hitkit.forgejo import Auth, Forgejo, ForgejoError
from hitkit.host.ca import create_ca


class FakeResponse:
    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class FakeUrlopen:
    """Records each Request and answers with `outcome` (a response or an exception)."""

    def __init__(self):
        self.requests = []
        self.outcome = FakeResponse(200, b"{}")

    def __call__(self, req, context=None, timeout=None):
        self.requests.append(req)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://forgejo.hitkit.internal/api/v1", code, "err", None, io.BytesIO(body))


@pytest.fixture
def server(monkeypatch):
    fake = FakeUrlopen()
    monkeypatch.setattr(forgejo.urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def client(tmp_path):
    ca = create_ca(ipaddress.IPv4Network("10.77.0.0/24"), "a" * 32)
    cafile = tmp_path / "ca.crt"
    cafile.write_bytes(ca.cert_pem)
    return Forgejo(cafile, Auth(token="x"))


# --- auth headers ----------------------------------------------------------


def test_token_header():
    assert Auth(token="x").header() == "token x"


def test_basic_header_is_base64_of_user_and_password():
    assert Auth(basic=("u", "p")).header() == "Basic " + base64.b64encode(b"u:p").decode()


def test_no_auth_has_no_header():
    assert Auth().header() is None


# --- client construction ---------------------------------------------------


def test_tls_is_verified_against_the_given_ca(client):
    assert client._ctx.verify_mode == ssl.CERT_REQUIRED
    assert client._ctx.check_hostname


def test_with_auth_returns_clone_and_leaves_original_alone(client):
    clone = client.with_auth(Auth(token="other"))
    assert clone.auth.token == "other"
    assert client.auth.token == "x"
    assert clone.base == client.base


# --- request ---------------------------------------------------------------


def test_get_parses_json_and_sends_auth(client, server):
    server.outcome = FakeResponse(200, b'{"login": "t"}')

    assert client.get("/user") == {"login": "t"}

    req = server.requests[0]
    assert req.full_url == "https://forgejo.hitkit.internal/api/v1/user"
    assert req.get_method() == "GET"
    assert req.get_header("Accept") == "application/json"
    assert req.get_header("Authorization") == "token x"


def test_basic_auth_is_sent_when_selected(client, server):
    client.with_auth(Auth(basic=("u", "p"))).get("/user")

    assert server.requests[0].get_header("Authorization") == "Basic " + base64.b64encode(b"u:p").decode()


def test_json_body_is_sent_with_content_type(client, server):
    server.outcome = FakeResponse(201, b'{"id": 1}')

    assert client.post("/orgs", {"username": "hitkit"}) == {"id": 1}

    req = server.requests[0]
    assert req.get_method() == "POST"
    assert req.data == b'{"username": "hitkit"}'
    assert req.get_header("Content-type") == "application/json"


def test_empty_body_returns_none(client, server):
    server.outcome = FakeResponse(204)
    assert client.delete("/users/x/tokens/y") is None


def test_http_error_raises_forgejo_error_with_status_and_message(client, server):
    server.outcome = _http_error(403, b'{"message":"nope"}')

    with pytest.raises(ForgejoError, match="nope") as info:
        client.get("/user")

    assert info.value.status == 403


def test_unreachable_host_raises_hitkit_error(client, server):
    server.outcome = urllib.error.URLError("connection refused")

    with pytest.raises(HitkitError, match="cannot reach Forgejo"):
        client.get("/user")


# --- exists ----------------------------------------------------------------


def test_exists_true_on_success(client, server):
    assert client.exists("/user") is True


def test_exists_false_on_404(client, server):
    server.outcome = _http_error(404, b'{"message":"not found"}')
    assert client.exists("/users/ghost") is False


def test_exists_reraises_other_errors(client, server):
    server.outcome = _http_error(500, b'{"message":"boom"}')

    with pytest.raises(ForgejoError) as info:
        client.exists("/users/x")

    assert info.value.status == 500
