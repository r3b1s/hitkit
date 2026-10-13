#!/usr/bin/env python3
"""Call the hitkit forge's REST API with a token that never appears in argv.

    forge_api.py METHOD PATH [BODY]

PATH is relative to /api/v1 (e.g. /user/repos). BODY is JSON text, @file, or
- for stdin. The token comes from FORGE_TOKEN_FILE (the JSON that
`hitkit token create --json` prints, or a bare token) or FORGE_TOKEN. TLS is
verified against the hitkit CA (HITKIT_CA, default ~/.local/share/hitkit/ca/ca.crt).

Prints the response (JSON pretty-printed, anything else as text). Exit codes:
0 for 2xx, 1 for an HTTP or connection error (the body goes to stderr), 2 for usage.
Standard library only.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://forgejo.hitkit.internal/api/v1"


class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body = status, body


def _token() -> str:
    path = os.environ.get("FORGE_TOKEN_FILE")
    if path:
        text = Path(path).expanduser().read_text().strip()
        try:
            return json.loads(text)["token"]
        except (ValueError, KeyError, TypeError):
            return text
    if os.environ.get("FORGE_TOKEN"):
        return os.environ["FORGE_TOKEN"]
    sys.exit("forge_api: set FORGE_TOKEN_FILE (or FORGE_TOKEN); see the hitkit-forge skill")


def _ca() -> str:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return os.environ.get("HITKIT_CA") or str(Path(data_home) / "hitkit" / "ca" / "ca.crt")


def call(method: str, path: str, body=None):
    """The decoded response: parsed JSON, text, or None for an empty body."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method.upper())
    req.add_header("Authorization", f"token {_token()}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(cafile=_ca()), timeout=60) as resp:
            raw = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace")) from None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw  # e.g. job logs are plain text


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    body = None
    if len(argv) == 3:
        text = sys.stdin.read() if argv[2] == "-" else (
            Path(argv[2][1:]).read_text() if argv[2].startswith("@") else argv[2])
        body = json.loads(text)
    try:
        out = call(argv[0], argv[1], body)
    except ApiError as e:
        print(f"forge_api: {argv[0].upper()} {argv[1]} -> HTTP {e.status}: {e.body}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"forge_api: cannot reach the forge: {e.reason} (is `hitkit up forge` done?)", file=sys.stderr)
        return 1
    if out is not None:
        print(out if isinstance(out, str) else json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
