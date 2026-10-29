"""`hitkit browser-cert`: a kit's login certificate into the user's NSS databases."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from hitkit import kits, secrets, trace
from hitkit.addressing import parse_subnet
from hitkit.errors import HitkitError
from hitkit.host import nssdb
from hitkit.host.ca import create_ca
from hitkit.machine.lifecycle import HostContext

pytestmark = pytest.mark.skipif(not (shutil.which("pk12util") and shutil.which("certutil")),
                                reason="needs the NSS tools (pk12util, certutil)")
SUBNET = parse_subnet("10.77.0.0/24")


@pytest.fixture
def hctx(user):
    cfg = SimpleNamespace(install_id="test", network=lambda: SUBNET)
    hctx = HostContext(user=user, cfg=cfg, ca=create_ca(SUBNET, "test"))
    secrets.put(user, "incus-os", "server.crt", "pinned")  # the kit is up
    return hctx


def new_db(path, password=None):
    path.mkdir(parents=True)
    if password is None:
        subprocess.run(["certutil", "-N", "-d", f"sql:{path}", "--empty-password"], check=True)
    else:
        pwfile = path.parent / f"{path.name}.pw"
        pwfile.write_text(password + "\n")
        subprocess.run(["certutil", "-N", "-d", f"sql:{path}", "-f", str(pwfile)], check=True)
    return path


def installed(db, nickname="hitkit incus-os"):
    return subprocess.run(["certutil", "-L", "-d", f"sql:{db}"], capture_output=True, text=True,
                          check=True).stdout.count(nickname)


def test_finds_firefox_profiles_and_the_shared_database(tmp_path):
    for db in (".pki/nssdb", ".mozilla/firefox/abc.default-release", ".config/mozilla/firefox/xyz.work",
               ".var/app/org.mozilla.firefox/.mozilla/firefox/f.default"):
        (tmp_path / db).mkdir(parents=True)
        (tmp_path / db / "cert9.db").touch()
    (tmp_path / ".mozilla/firefox/Crash Reports").mkdir()  # not a profile

    assert [p.relative_to(tmp_path).as_posix() for p in nssdb.databases(tmp_path)] == [
        ".pki/nssdb", ".mozilla/firefox/abc.default-release", ".config/mozilla/firefox/xyz.work",
        ".var/app/org.mozilla.firefox/.mozilla/firefox/f.default"]


def test_import_is_idempotent_and_replaces_a_regenerated_certificate(hctx, tmp_path):
    hooks = kits.hooks("incus-os")
    db = new_db(tmp_path / "db")
    cert = hooks.browser_certificate(hctx)

    assert nssdb.import_bundle(db, cert["bundle"], cert["password"], cert["nickname"], cert["certificate"]) == "imported"
    assert nssdb.import_bundle(db, cert["bundle"], cert["password"], cert["nickname"], cert["certificate"]) == "present"
    assert installed(db) == 1

    for name in ("browser.crt", "browser.key"):  # what a destroy --delete-data and a new up amount to
        secrets.delete(hctx.user, "incus-os", name)
    (secrets.kit_dir(hctx.user, "incus-os") / "browser.p12").unlink()
    new = hooks.browser_certificate(hctx)
    assert new["certificate"] != cert["certificate"]
    assert nssdb.import_bundle(db, new["bundle"], new["password"], new["nickname"], new["certificate"]) == "replaced"
    assert installed(db) == 1

    assert nssdb.remove(db, cert["nickname"]) is True
    assert installed(db) == 0
    assert nssdb.remove(db, cert["nickname"]) is False


def test_the_bundle_password_never_appears_in_a_command_line(hctx, tmp_path, monkeypatch):
    cert = kits.hooks("incus-os").browser_certificate(hctx)
    argvs = []
    real = trace.run
    monkeypatch.setattr(trace, "run", lambda cmd, **kw: (argvs.append(list(cmd)), real(cmd, **kw))[1])

    nssdb.import_bundle(new_db(tmp_path / "db"), cert["bundle"], cert["password"], cert["nickname"],
                        cert["certificate"])

    assert any(a[0] == "pk12util" for a in argvs)
    assert not any(cert["password"] in arg for a in argvs for arg in a)


def test_a_database_with_a_primary_password_is_reported_not_waited_on(hctx, tmp_path):
    cert = kits.hooks("incus-os").browser_certificate(hctx)
    db = new_db(tmp_path / "db", password="primary")
    with pytest.raises(HitkitError, match="primary password") as e:
        nssdb.import_bundle(db, cert["bundle"], cert["password"], cert["nickname"], cert["certificate"])
    assert "browser's own dialog" in e.value.hint


def test_kits_without_a_browser_certificate_say_so(hctx):
    assert kits.hooks("forge").browser_certificate(hctx) is None


def test_the_command_imports_into_each_database_and_reports_failures(hctx, tmp_path, monkeypatch):
    from hitkit import cli

    monkeypatch.setattr(cli, "_host_context", lambda: hctx)
    ok, locked = new_db(tmp_path / "ok"), new_db(tmp_path / "locked", password="primary")

    result = CliRunner().invoke(cli.cli, ["--json", "browser-cert", "incus-os", "--db", str(ok), "--db", str(locked)])

    assert result.exit_code != 0
    reported, _ = json.JSONDecoder().raw_decode(result.output)  # then the error, as its own document
    assert [(r["db"], r["result"]) for r in reported] == [(str(ok), "imported"), (str(locked), "failed")]
    assert installed(ok) == 1

    removed = CliRunner().invoke(cli.cli, ["browser-cert", "incus-os", "--remove", "--db", str(ok)])
    assert removed.exit_code == 0 and removed.output.startswith("removed")
    assert installed(ok) == 0
