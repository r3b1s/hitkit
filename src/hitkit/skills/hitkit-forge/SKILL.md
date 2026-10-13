---
name: hitkit-forge
description: Work with the local Forgejo forge that hitkit provisions (forgejo.hitkit.internal) - mint scoped API tokens, create repositories, push over SSH or HTTPS, run Forgejo Actions CI on persistent or ephemeral runner VMs, enroll repos in Renovate, grant the hitkit-dev account access, and push-mirror to GitHub or GitLab. Use when a task involves the hitkit forge, its CI, or its git remotes.
compatibility: Requires `hitkit up forge` to have completed on this host, plus python3 and git.
---

# hitkit forge

The `forge` kit runs Forgejo at `https://forgejo.hitkit.internal`
(API: `https://forgejo.hitkit.internal/api/v1`), CI runner VMs and a Renovate
bot. Read the `hitkit` skill's rules first: use `--json`, keep secrets out of
argv and chat, and ask before anything destructive or outward-facing.

```sh
hitkit status forge --json        # forgejo and runner-N should be "running"
hitkit up forge                   # (re)wire everything; idempotent, keeps knobs
```

TLS uses the hitkit CA, `~/.local/share/hitkit/ca/ca.crt` (the
`ca_certificate` field of `hitkit creds forge --json`). Never disable
verification. `creds` also prints the human's password. Drop it before
looking: `hitkit creds forge --json | jq 'del(.password)'`.

Your shell state may not survive between commands, so everything below keeps
state in files and uses the bundled scripts. They need only `python3`, and run
from this skill's directory: `scripts/forge_api.py` and `scripts/run_workflow.py`.

## API access: mint your own token

```sh
umask 077; mkdir -p ~/.cache/agent && export FORGE_TOKEN_FILE=~/.cache/agent/forge-token.json
hitkit token create forge --name agent-<task> --scope write:repository --scope write:user --json > "$FORGE_TOKEN_FILE"
```

- The file holds `{"name", "token", "scopes", "user"}`. The token is shown
  only this once and acts as the human's forge account (`user`).
- Every later command needs `FORGE_TOKEN_FILE` set again (or prefix it to
  the command).
- Without `--scope`, you get the defaults: `write:repository`, `write:issue`,
  `write:package`, `read:user`, `read:organization`. Any `--scope` replaces the
  defaults entirely. Forgejo also drops a `read:X` that a `write:X` already
  covers.
- Request only what the task needs:

| Task | Scope |
|---|---|
| create a repo under the user (`POST /user/repos`) | `write:user` (plus `write:repository`) |
| push, read and delete repos, run and watch CI | `write:repository` |
| issues and pull requests | `write:issue` |
| packages and the container registry | `write:package` |

The API takes PATH relative to `https://forgejo.hitkit.internal/api/v1`. BODY
is JSON text, `@file`, or `-` for stdin. Exit 1 means an HTTP error, and the
body goes to stderr.

```sh
scripts/forge_api.py GET /user                                         # who am I
scripts/forge_api.py POST /user/repos '{"name": "demo", "default_branch": "main", "auto_init": true}'
scripts/forge_api.py GET /repos/<owner>/demo
scripts/forge_api.py DELETE /repos/<owner>/demo                        # cleanup (write:repository)
```

When the task is done, revoke the token and delete the file:

```sh
hitkit token revoke forge agent-<task> --json                          # {"revoked": "agent-<task>"}
hitkit token list forge --json                                         # [{"name", "scopes", "last_eight"}]
rm -f ~/.cache/agent/forge-token.json
```

## Git

- **SSH (simplest):** `git clone forgejo.hitkit.internal:<owner>/<repo>.git`.
  hitkit installs an `Include` in `~/.ssh/config` with the forge's pinned host
  key (port 2222) and the user's hitkit key. If the include is missing, use
  `GIT_SSH_COMMAND="ssh -F ~/.config/hitkit/ssh_config"`. This acts as the
  human user.
- **HTTPS as your token:** git reads the header from its environment, so the
  token never appears in argv, a URL, or `.git/config`:

  ```sh
  GIT_SSL_CAINFO=~/.local/share/hitkit/ca/ca.crt GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=http.https://forgejo.hitkit.internal/.extraHeader \
    GIT_CONFIG_VALUE_0="Authorization: token $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$FORGE_TOKEN_FILE")" \
    git clone https://forgejo.hitkit.internal/<owner>/<repo>.git    # same prefix for push and fetch
  ```

## CI (Forgejo Actions)

Workflows live in `.forgejo/workflows/*.yml` and use GitHub Actions syntax.

| `runs-on` | Where the job runs |
|---|---|
| `ubuntu-latest`, `ubuntu-24.04` | A container (`catthehacker/ubuntu:act-24.04`) on a persistent `runner-N` VM |
| `ephemeral` | A fresh single-use VM, destroyed after the job. Needs `hitkit up forge --ephemeral N`. With `--runners 0`, the pool also takes the ubuntu labels. |
| `arch` | A fresh single-use **Arch Linux VM**. The job runs directly on it, as `runner` with passwordless sudo, pacman, Docker, git, Node and Python. Needs `hitkit up forge --ephemeral N --arch`. `services:` is ignored here, so start containers with `docker run -d` in a step. Extra baked-in packages go in `~/.config/hitkit/images/runner-arch.toml` (`packages = [...]`), followed by `hitkit up forge`. |

Inside jobs, already configured:
- The hitkit CA reaches `actions/checkout`, Node-based actions and git (for
  forge URLs).
- `/etc/hitkit/ca-bundle.crt` holds the public roots plus the hitkit CA, with
  `SSL_CERT_FILE`, `PIP_CERT` and `REQUESTS_CA_BUNDLE` set to it.
- If the `cache` kit is up: `PIP_INDEX_URL`, `UV_DEFAULT_INDEX`,
  `NPM_CONFIG_REGISTRY`, and Docker Hub pulls through the cache.

For other tools, point them at `/etc/hitkit/ca.crt` or the bundle.

**Run a workflow and wait for it.** It needs `on: workflow_dispatch`, and the
token needs `write:repository`:

```sh
scripts/run_workflow.py <owner>/<repo> ci.yml [--ref main] [--timeout 900] [--logs]
```

The script prints `{"id", "index", "status", "html_url", "jobs": [...]}`, and
failed jobs include the end of their log. It exits 0 only on `success`.

To do the same by hand:
- Record the existing run ids.
- `POST /repos/<o>/<r>/actions/workflows/<file>/dispatches {"ref": "main"}`.
  It returns an empty body.
- Poll `GET /repos/<o>/<r>/actions/runs` every 5 s for the new id, until its
  `status` leaves `waiting`/`running` for one of `success`, `failure`,
  `cancelled` or `skipped`.
- Then call `GET …/actions/runs/<id>/jobs` and
  `GET …/actions/jobs/<job-id>/logs` (plain text).
- Use the run's `id` in the API. The number at the end of `html_url` is the
  per-repository index, which the API does not accept.

A push-triggered run (`on: push`) appears the same way. Find it as the newest
run whose `commit_sha` is your commit. A first job on a fresh runner pulls its
image, which takes longer.

Ephemeral pool health: `hitkit pool status --json` returns `enabled`, `max`,
`labels`, `service_active` and the `job_vms`. Pool decisions are recorded in
`hitkit trace` (command `pool run`).

## Access for other accounts

```sh
hitkit grant <owner>/<repo> hitkit-dev                 # dev VMs can clone/push this repo (default write)
hitkit grant <owner>/<repo> renovate-bot               # enroll in Renovate (runs hourly from hitkit/renovate)
hitkit grant <owner>/<repo> <account> --permission read
```

## Push mirrors (outward-facing: confirm with the user)

```sh
# The token comes from the user (env var or stdin); never put it on the command line.
HITKIT_MIRROR_TOKEN=… hitkit mirror add <owner>/<repo> https://github.com/<org>/<repo>.git
hitkit mirror list <owner>/<repo> --json               # last sync time and last error
hitkit mirror sync <owner>/<repo>                      # push now
hitkit mirror remove <owner>/<repo> <mirror-name>
```

The username is guessed for github.com (`x-access-token`) and GitLab
(`oauth2`); pass `--username` otherwise. Mirrors sync on every push and every
`--interval` (default 8h). The remote repository must already exist.

## Failures

API errors exit 1 with `Forgejo API error <status>: <message>` and a trace id.
`hitkit trace <id>` shows each API call with its status and error body. A
failing CI job is a problem in the workflow, not in hitkit, unless the runner
is down (`hitkit status forge`) or no runner serves the label. See
`hitkit-troubleshoot`.
