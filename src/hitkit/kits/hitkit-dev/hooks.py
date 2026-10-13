"""hitkit-dev: give the dev VM least-privilege access to the local forge.

The VM gets its own Forgejo account (`hitkit-dev`) and token, delivered as a
credential; you decide which repositories it can reach by adding it as a
collaborator (`hitkit grant <owner/repo> hitkit-dev`). Nothing from the host
filesystem is shared and your own token never enters the VM.
"""

from __future__ import annotations

from hitkit import kits, secrets
from hitkit.errors import HitkitError
from hitkit.forgejo import FORGE_URL, ForgejoError
from hitkit.kits import Hooks

ACCOUNT = "hitkit-dev"
SCOPES = ["write:repository", "write:issue", "read:user", "read:organization", "read:package"]


class KitHooks(Hooks):
    def credentials(self, hctx, machine, plan):
        u = hctx.user
        if secrets.get(u, "forge", "admin.token") is None:
            return {}  # no forge on this host (yet)
        token = secrets.get(u, "forge", f"{ACCOUNT}.token")
        forge = kits.hooks("forge")
        try:
            admin = forge.admin_api(hctx)
        except HitkitError:
            # Forge temporarily unreachable: keep whatever access was granted before.
            return self._files(token) if token else {}
        password = forge._ensure_user(hctx, admin, ACCOUNT, f"{ACCOUNT}@hitkit.internal", f"{ACCOUNT}.password")
        if token is None or not forge._token_valid(hctx, token):
            token = forge._mint_token(forge._api(hctx), ACCOUNT, password, "hitkit-dev-vm", SCOPES)
            secrets.put(u, "forge", f"{ACCOUNT}.token", token)
        return self._files(token)

    @staticmethod
    def _files(token: str) -> dict[str, bytes]:
        return {"hitkit.dev.forge-user": ACCOUNT.encode(), "hitkit.dev.forge-token": token.encode()}

    def creds(self, hctx, machine: str = "dev") -> dict:
        data = {"ssh": f"hitkit ssh -u dev {machine}", "nested_subnet": "10.78.0.0/24"}
        if secrets.get(hctx.user, "forge", f"{ACCOUNT}.token"):
            data |= {
                "forge_account": ACCOUNT,
                "forge_access": f"grant per repository: hitkit grant <owner/repo> {ACCOUNT}",
                "clone_in_vm": f"git clone {FORGE_URL}/<owner>/<repo>.git",
            }
        return data
