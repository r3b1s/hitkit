"""Wiring for the forge kit (design §9.3).

Bring-up order: forgejo boots with its secrets as credentials; once it is
ready, hitkit registers each runner with a shared secret, mints its own admin
token, and creates the human user. Runner VMs then boot holding only their own
runner token. Finally Renovate is wired up as a scheduled workflow.
"""

from __future__ import annotations

import base64
import json
import uuid

from hitkit import secrets, sshconfig
from hitkit.errors import HitkitError
from hitkit.forgejo import Auth, Forgejo, ForgejoError, FORGE_URL
from hitkit.host.ca import ca_cert_path
from hitkit.kits import Hooks
from hitkit.machine import creds as machine_creds
from hitkit.machine import remote

KIT = "forge"
ARCH_ROLE, ARCH_LABEL = "runner-arch", "arch"  # the Arch job VM flavor (knob --arch)
ADMIN = "hitkit-admin"
BOT = "renovate-bot"
ORG = "hitkit"
RENOVATE_REPO = "renovate"
RENOVATE_IMAGE = (
    "ghcr.io/renovatebot/renovate:44.115.13"
    "@sha256:0c04185c9e7ea5284da22ea0b2a2c88dcd3687badd8feb6c71691659b380df9b"
)
ADMIN_SCOPES = [
    "write:activitypub", "write:admin", "write:issue", "write:misc", "write:notification",
    "write:organization", "write:package", "write:repository", "write:user",
]
BOT_SCOPES = ["write:repository", "write:issue", "read:user", "read:organization", "read:package", "read:misc"]
DEFAULT_USER_SCOPES = ["write:repository", "write:issue", "write:package", "read:user", "read:organization"]

RENOVATE_WORKFLOW = f"""\
# Managed by hitkit. Renovate runs hourly over every repository renovate-bot
# can access: add renovate-bot as a collaborator (write) to opt a repo in.
name: renovate
on:
  schedule:
    - cron: "17 * * * *"
  workflow_dispatch:
jobs:
  renovate:
    runs-on: ubuntu-latest
    container:
      image: {RENOVATE_IMAGE}
    steps:
      - name: Run Renovate
        # Renovate runs git with a scrubbed environment, so the runner's
        # GIT_CONFIG_* CA settings do not reach it; set the same per-URL trust
        # (forge only) in the container's global git config instead.
        run: |
          git config --global http.https://forgejo.hitkit.internal/.sslCAInfo /etc/hitkit/ca.crt
          renovate
        env:
          RENOVATE_PLATFORM: forgejo
          RENOVATE_ENDPOINT: ${{{{ github.server_url }}}}/api/v1
          RENOVATE_TOKEN: ${{{{ secrets.RENOVATE_TOKEN }}}}
          RENOVATE_AUTODISCOVER: "true"
          RENOVATE_ONBOARDING: "false"
          RENOVATE_REQUIRE_CONFIG: optional
          RENOVATE_GIT_AUTHOR: "Renovate Bot <{BOT}@hitkit.internal>"
          LOG_LEVEL: info
"""


def runner_uuid(secret: str) -> str:
    """forgejo-cli derives a runner's UUID from the first 16 characters of its secret."""
    return str(uuid.UUID(bytes=secret[:16].encode()))


def human_username(hctx) -> str:
    name = "".join(c for c in hctx.user.name.lower() if c.isalnum() or c in "-_.")
    return name if name and name not in (ADMIN, BOT, ORG) else "developer"


class KitHooks(Hooks):
    # --- backups -----------------------------------------------------------

    def backup_plan(self, machine):
        """Forgejo's data (repositories, SQLite, LFS, packages) is copied live with Forgejo
        stopped for consistency: a short outage instead of a stopped VM. Runner and job VMs
        hold only caches and their registration, which `up` recreates."""
        from hitkit.backup import BackupPlan

        if machine.name == "forgejo":
            return BackupPlan("online", ("forgejo.service",))
        return None

    # --- credentials -------------------------------------------------------

    def credentials(self, hctx, machine, plan):
        u = hctx.user
        if machine.role == "forgejo":
            gen = {
                "secret-key": secrets.hex_secret(32),
                "internal-token": secrets.b64url_32,
                "jwt-secret": secrets.b64url_32,
                "lfs-jwt-secret": secrets.b64url_32,
                "admin-password": secrets.password,
            }
            files = {
                f"hitkit.fj.{name}": secrets.get_or_create(u, KIT, f"forgejo.{name}", g).encode()
                for name, g in gen.items()
            }
            files["hitkit.fj.git-host-key"] = self._git_host_key(hctx)[0].encode()
            return files
        if machine.role == "runner":
            secret = self._runner_secret(hctx, machine.name)
            return {"hitkit.runner.token": secret.encode(), "hitkit.runner.uuid": runner_uuid(secret).encode()} \
                | cache_credentials(hctx)
        return {}

    def _git_host_key(self, hctx) -> tuple[str, str]:
        """(private OpenSSH, public line) for Forgejo's SSH server; pinned on the client side."""
        u = hctx.user
        if secrets.get(u, KIT, "forgejo.git-host-key") is None:
            pair = machine_creds.ssh_keypair()
            secrets.put(u, KIT, "forgejo.git-host-key", pair.private_openssh.decode())
            secrets.put(u, KIT, "forgejo.git-host-key.pub", pair.public_line)
        return secrets.get(u, KIT, "forgejo.git-host-key"), secrets.get(u, KIT, "forgejo.git-host-key.pub")

    def _user_git_key(self, hctx) -> str:
        """Dedicated key for the human user's git over SSH. Returns its public line."""
        path = sshconfig.key_path(hctx.user)
        if not path.exists():
            pair = machine_creds.ssh_keypair()
            from hitkit.paths import write_private
            write_private(path, pair.private_openssh, hctx.user, mode=0o600)
            write_private(path.with_suffix(".pub"), f"{pair.public_line} hitkit-forge\n".encode(), hctx.user, mode=0o644)
        return path.with_suffix(".pub").read_text().strip()

    def _runner_secret(self, hctx, name: str) -> str:
        return secrets.get_or_create(hctx.user, KIT, f"{name}.secret", secrets.hex_secret(20))

    # --- API helpers -------------------------------------------------------

    def _api(self, hctx, auth: Auth | None = None) -> Forgejo:
        return Forgejo(ca_cert_path(hctx.user), auth or Auth())

    def admin_api(self, hctx) -> Forgejo:
        u = hctx.user
        token = secrets.get(u, KIT, "admin.token")
        api = self._api(hctx)
        if token:
            try:
                api.with_auth(Auth(token=token)).get("/user")
                return api.with_auth(Auth(token=token))
            except ForgejoError as e:
                if e.status not in (401, 403):
                    raise
        password = secrets.get(u, KIT, "forgejo.admin-password")
        token = self._mint_token(api, ADMIN, password, "hitkit", ADMIN_SCOPES)
        secrets.put(u, KIT, "admin.token", token)
        return api.with_auth(Auth(token=token))

    @staticmethod
    def _mint_token(api: Forgejo, user: str, password: str, name: str, scopes: list[str]) -> str:
        basic = api.with_auth(Auth(basic=(user, password)))
        basic.delete(f"/users/{user}/tokens/{name}")
        return basic.post(f"/users/{user}/tokens", {"name": name, "scopes": scopes})["sha1"]

    def _ensure_user(self, hctx, admin: Forgejo, username: str, email: str, secret_name: str) -> str:
        password = secrets.get_or_create(hctx.user, KIT, secret_name, secrets.password)
        if not admin.exists(f"/users/{username}"):
            admin.post("/admin/users", {
                "username": username, "email": email, "password": password,
                "must_change_password": False, "send_notify": False,
            })
        return password

    # --- bring-up ----------------------------------------------------------

    def after_ready(self, hctx, machine, plan, knobs, log):
        if machine.role != "forgejo":
            return
        for runner in (m for m in plan if m.role == "runner"):
            # --keep-labels: re-registering must not wipe the labels the running
            # runner declared, or jobs stop matching it until it restarts.
            log(f"registering {runner.name} with Forgejo")
            remote.run(
                hctx, machine.name,
                "runuser -u forgejo -- env HOME=/srv/data/forgejo FORGEJO_WORK_DIR=/srv/data/forgejo "
                "forgejo --config /etc/forgejo/app.ini forgejo-cli actions register "
                f"--name {runner.name} --keep-labels --secret-stdin -",
                stdin=self._runner_secret(hctx, runner.name),
            )
        admin = self.admin_api(hctx)
        user = human_username(hctx)
        log(f"ensuring Forgejo user '{user}'")
        self._ensure_user(hctx, admin, user, f"{user}@hitkit.internal", "user.password")
        self._ensure_git_ssh(hctx, admin, user, log)

    def _ensure_git_ssh(self, hctx, admin: Forgejo, user: str, log) -> None:
        pub = self._user_git_key(hctx)
        key_body = " ".join(pub.split()[:2])
        if not any(" ".join(k["key"].split()[:2]) == key_body for k in admin.get(f"/users/{user}/keys")):
            admin.post(f"/admin/users/{user}/keys", {"title": "hitkit", "key": pub, "read_only": False})
        sshconfig.install(hctx.user, self._git_host_key(hctx)[1])
        log(f"git over SSH: git clone {sshconfig.FORGE_HOST}:{user}/<repo>.git (via ~/.ssh/config include)")

    def finish(self, hctx, plan, knobs, log):
        if knobs.get("renovate"):
            log("wiring Renovate (hitkit/renovate)")
            self._renovate(hctx)
        self._configure_pool(hctx, knobs, log)

    # --- ephemeral runner pool -------------------------------------------------

    def _configure_pool(self, hctx, knobs, log) -> None:
        from dataclasses import asdict

        from hitkit import forgepool, kits, virt

        wanted = knobs.get("ephemeral", 0)
        if knobs.get("arch") and wanted == 0:
            raise HitkitError("Arch job VMs run in the ephemeral pool, which is off",
                              hint="hitkit up forge --ephemeral 2 --arch")
        if wanted == 0:
            if forgepool.load_config(hctx.user) is not None:
                log("disabling the ephemeral runner pool")
                self.before_down(hctx)
                secrets.put(hctx.user, KIT, "pool.json", "{}")
            return
        kit = kits.load(KIT)
        runner = next(t.spec for t in kit.templates if t.spec.role == "runner")
        sizing = secrets.get(hctx.user, KIT, "sizing.json")  # job VMs are sized like runners
        sized = kits.apply_sizing(kit, (runner,), json.loads(sizing) if sizing else None)[0]  # as runner-1
        base = replace_spec(sized, name="ci-1")
        prepull = [RENOVATE_IMAGE] if knobs.get("renovate") and knobs.get("runners", 1) == 0 else []
        with virt.connect(virt.SESSION_URI) as conn:
            template = forgepool.ensure_template(conn, hctx, base, prepull, log)
        flavors = [{"labels": forgepool.labels_for(knobs.get("runners", 1)), "spec": asdict(base),
                    "template": str(template)}]
        if knobs.get("arch"):
            from hitkit.machine import images

            # Built here rather than by the pool's first Arch job, which would wait minutes.
            images.ensure(hctx.user, ARCH_ROLE, log)
            flavors.append({"labels": [ARCH_LABEL], "spec": asdict(replace_spec(base, role=ARCH_ROLE)),
                            "template": None})  # a fresh, blank data disk per job
        forgepool.save_config(hctx.user, {"max": wanted, "flavors": flavors})
        forgepool.install_service(hctx.user)
        labels = [label for f in flavors for label in f["labels"]]
        log(f"ephemeral pool: up to {wanted} job VM(s) for labels {', '.join(labels)} ({forgepool.UNIT_NAME})")

    def before_down(self, hctx) -> None:
        """Stop the pool and remove its job VMs (they are single-use anyway)."""
        from hitkit import forgepool, virt

        forgepool.remove_service(hctx.user)
        if secrets.get(hctx.user, KIT, "admin.token") is None:
            return
        with virt.connect(virt.SESSION_URI) as conn:
            try:
                forgepool.Pool(hctx, self.admin_api(hctx), lambda m: None).drain(conn)
            except HitkitError:
                pass  # forge already down: the VMs are removed by destroy

    def _renovate(self, hctx) -> None:
        admin = self.admin_api(hctx)
        u = hctx.user
        bot_password = self._ensure_user(hctx, admin, BOT, f"{BOT}@hitkit.internal", "renovate.password")
        token = secrets.get(u, KIT, "renovate.token")
        if token is None or not self._token_valid(hctx, token):
            token = self._mint_token(self._api(hctx), BOT, bot_password, "renovate", BOT_SCOPES)
            secrets.put(u, KIT, "renovate.token", token)

        if not admin.exists(f"/orgs/{ORG}"):
            admin.post("/orgs", {"username": ORG, "full_name": "hitkit", "visibility": "public"})
        human = human_username(hctx)
        owners = next(t for t in admin.get(f"/orgs/{ORG}/teams") if t["name"] == "Owners")
        admin.put(f"/teams/{owners['id']}/members/{human}")
        if not admin.exists(f"/repos/{ORG}/{RENOVATE_REPO}"):
            admin.post(f"/orgs/{ORG}/repos", {
                "name": RENOVATE_REPO, "auto_init": True, "default_branch": "main",
                "description": "Managed by hitkit: scheduled Renovate runs",
            })
        self._put_file(admin, ORG, RENOVATE_REPO, ".forgejo/workflows/renovate.yml", RENOVATE_WORKFLOW)
        admin.put(f"/orgs/{ORG}/actions/secrets/RENOVATE_TOKEN", {"data": token})

    def _token_valid(self, hctx, token: str) -> bool:
        try:
            self._api(hctx, Auth(token=token)).get("/user")
            return True
        except ForgejoError:
            return False

    @staticmethod
    def _put_file(api: Forgejo, owner: str, repo: str, path: str, content: str) -> None:
        encoded = base64.b64encode(content.encode()).decode()
        try:
            current = api.get(f"/repos/{owner}/{repo}/contents/{path}")
        except ForgejoError as e:
            if e.status != 404:
                raise
            api.post(f"/repos/{owner}/{repo}/contents/{path}", {"content": encoded, "message": "hitkit: add Renovate workflow"})
            return
        if base64.b64decode(current["content"]).decode() != content:
            api.put(f"/repos/{owner}/{repo}/contents/{path}", {
                "content": encoded, "sha": current["sha"], "message": "hitkit: update Renovate workflow",
            })

    # --- user-facing -------------------------------------------------------

    def creds(self, hctx) -> dict:
        u = hctx.user
        password = secrets.get(u, KIT, "user.password")
        if password is None:
            raise HitkitError("the forge kit has not been brought up yet", hint="hitkit up forge")
        return {
            "url": FORGE_URL + "/",
            "username": human_username(hctx),
            "password": password,
            "git_clone_https": f"{FORGE_URL}/<owner>/<repo>.git",
            "git_clone_ssh": f"{sshconfig.FORGE_HOST}:<owner>/<repo>.git",
            "ssh_config": str(sshconfig.snippet_path(u)),
            "git_ssh_command": f"ssh -F {sshconfig.snippet_path(u)}",
            "ca_certificate": str(ca_cert_path(u)),
            "renovate_bot": BOT,
        }

    def _user_api(self, hctx) -> tuple[Forgejo, str]:
        info = self.creds(hctx)
        return self._api(hctx, Auth(basic=(info["username"], info["password"]))), info["username"]

    def token_create(self, hctx, name: str, scopes: list[str] | None) -> dict:
        api, user = self._user_api(hctx)
        tok = api.post(f"/users/{user}/tokens", {"name": name, "scopes": scopes or DEFAULT_USER_SCOPES})
        return {"name": tok["name"], "token": tok["sha1"], "scopes": tok.get("scopes", []), "user": user}

    def token_list(self, hctx) -> list[dict]:
        api, user = self._user_api(hctx)
        return [{"name": t["name"], "scopes": t.get("scopes", []), "last_eight": t.get("token_last_eight")}
                for t in api.get(f"/users/{user}/tokens")]

    def token_revoke(self, hctx, name: str) -> bool:
        api, user = self._user_api(hctx)
        return api.request("DELETE", f"/users/{user}/tokens/{name}", ok=(204, 404)) is None

    # --- teardown ------------------------------------------------------------

    def destroyed(self, hctx) -> None:
        from hitkit import forgepool

        forgepool.remove_service(hctx.user)
        sshconfig.uninstall(hctx.user)

    # --- access grants and push mirrors --------------------------------------

    def grant(self, hctx, repo: str, account: str, permission: str) -> None:
        self.admin_api(hctx).put(f"/repos/{repo}/collaborators/{account}", {"permission": permission})

    def mirror_add(self, hctx, repo: str, remote: str, username: str | None, password: str,
                   interval: str, sync_on_commit: bool) -> dict:
        if username is None:
            username = default_mirror_username(remote)
        return self.admin_api(hctx).post(f"/repos/{repo}/push_mirrors", {
            "remote_address": remote, "remote_username": username, "remote_password": password,
            "interval": interval, "sync_on_commit": sync_on_commit,
        })

    def mirror_list(self, hctx, repo: str) -> list[dict]:
        return [
            {"name": m["remote_name"], "remote": m["remote_address"], "interval": m.get("interval"),
             "last_update": m.get("last_update"), "last_error": m.get("last_error") or None,
             "sync_on_commit": m.get("sync_on_commit")}
            for m in self.admin_api(hctx).get(f"/repos/{repo}/push_mirrors")
        ]

    def mirror_remove(self, hctx, repo: str, name: str) -> None:
        self.admin_api(hctx).delete(f"/repos/{repo}/push_mirrors/{name}")

    def mirror_sync(self, hctx, repo: str) -> None:
        self.admin_api(hctx).post(f"/repos/{repo}/push_mirrors-sync")


CACHE_URL = "https://cache.hitkit.internal"


def cache_credentials(hctx) -> dict[str, bytes]:
    """Point runners at the pull-through caches when the `cache` kit is up."""
    return {"hitkit.runner.cache": CACHE_URL.encode()} if hctx.paths("cache").known_hosts.exists() else {}


def replace_spec(spec, **changes):
    from dataclasses import replace

    return replace(spec, **changes)


def default_mirror_username(remote: str) -> str:
    """Token-auth usernames the big forges accept; anything else must be given explicitly."""
    from urllib.parse import urlparse

    host = urlparse(remote).hostname or ""
    if host == "github.com":
        return "x-access-token"
    if host == "gitlab.com" or host.startswith("gitlab."):
        return "oauth2"
    raise HitkitError(f"cannot guess the username for {host or remote}", hint="pass --username")
