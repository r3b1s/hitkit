# Kit reference

`hitkit kits --json` is authoritative for machines, default sizes and knobs;
`hitkit creds <kit> --json` for endpoints and logins. This page explains them.

## devbox

- Machine `devbox` (Debian 13), with SSH and a persistent ext4 data disk at `/srv/data`.
- Access: `hitkit ssh devbox` (root). Anything outside `/srv/data` is
  replaced on `hitkit upgrade devbox`.

## forge

- Machines: `forgejo` (Forgejo, web and API at `https://forgejo.hitkit.internal`,
  git over SSH on port 2222) and `runner-1`… (Forgejo Actions runners with
  Docker).
- Knobs: `--runners N` (0–10 persistent runner VMs, default 1),
  `--ephemeral N` (0–20 single-use job VMs, default 0), `--[no-]arch` (Arch
  Linux job VMs for `runs-on: arch` in that pool, default off) and
  `--[no-]renovate` (a scheduled Renovate bot, default on). Knobs persist
  between `up` runs.
- `creds` keys: `url`, `username`, `password` (the human's login), `git_clone_https`,
  `git_clone_ssh`, `ssh_config`, `git_ssh_command`, `ca_certificate` and `renovate_bot`.
- Everything else (tokens, repositories, CI, mirrors, grants) is in the
  `hitkit-forge` skill.

## cache

Machine `cache`, which caches what CI jobs and machines download.

| Endpoint | Caches |
|---|---|
| `https://cache.hitkit.internal:5000` | Docker Hub (OCI registry mirror) |
| `https://cache.hitkit.internal:5001` | ghcr.io |
| `https://cache.hitkit.internal:5002/index/` | PyPI (pip `--index-url`, uv `UV_DEFAULT_INDEX`) |
| `https://cache.hitkit.internal:5003` | npm registry |
| `http://cache.hitkit.internal:3142` | apt (apt-cacher-ng proxy) |

When `cache` exists, forge runners and job VMs use it automatically: Docker
pulls through the mirror, and jobs get `PIP_INDEX_URL`, `UV_DEFAULT_INDEX` and
`NPM_CONFIG_REGISTRY`. After bringing `cache` up, re-run `hitkit up forge` to
wire it in. TLS uses the hitkit CA.

## proxmox

- Machine `proxmox`: Proxmox VE 9 with nested KVM. It is `stateful_os` (VM
  definitions live on its OS disk): `upgrade` refuses, so update it in place. The web UI and API are at
  `https://proxmox.hitkit.internal:8006`.
- `creds` keys: `url`, `username` (`root@pam`), `password`, `api_token`
  (`root@pam!hitkit=<uuid>`), `api_header` (a ready-made `Authorization:`
  header value) and `storage` (directory storage `data` on the 100 GiB data disk).
- API example (pass the header on stdin so it stays out of argv; rule 3):

  ```sh
  hitkit creds proxmox --json | jq -r '"header = \"" + .api_header + "\""' \
    | curl -sS --cacert ~/.local/share/hitkit/ca/ca.crt -K - https://proxmox.hitkit.internal:8006/api2/json/version
  ```

## pbs

- Machine `pbs`: Proxmox Backup Server 4 on Debian 13, with the datastore `data`
  on the 100 GiB data disk. It is `stateful_os` (users, tokens and datastores live
  on its OS disk): `upgrade` refuses, so update it in place. The web UI and API are
  at `https://pbs.hitkit.internal:8007`.
- `creds` keys: `url`, `username` (`root@pam`), `password`, `api_token`
  (`root@pam!hitkit:<secret>`), `api_header` (`Authorization: PBSAPIToken=…`),
  `datastore`, `repository` (for `proxmox-backup-client`: set `PBS_REPOSITORY` and
  put the token secret in `PBS_PASSWORD`, never in argv) and `ssh`.
- Knob `--proxmox`: add the datastore to the `proxmox` kit's Proxmox VE as backup
  storage `pbs`, using a separate backup-only user. Needs `proxmox` up; re-run
  `hitkit up pbs --proxmox` after bringing `proxmox` up later.
- API example (header on stdin, as for `proxmox`):

  ```sh
  hitkit creds pbs --json | jq -r '"header = \"" + .api_header + "\""' \
    | curl -sS --cacert ~/.local/share/hitkit/ca/ca.crt -K - https://pbs.hitkit.internal:8007/api2/json/version
  ```

## ludus

- Machine `ludus` (default 8 vCPU, 32 GiB, 250 GiB data): Proxmox VE 9 with
  Ludus 2.3.8. Ludus is installed on first boot, which needs internet access
  and takes many minutes; watch with `hitkit logs ludus -f`.
- The API is at `https://ludus.hitkit.internal:8080` and the Proxmox UI at
  `:8006`. Both use the hitkit CA.
- `creds` keys: `url`, `user_id`, `api_key`, `password`, `client_env`,
  `wireguard`, `proxmox_ui`, `proxmox_root_password`, `ca_certificate`.
- Use the client inside the VM: `hitkit ssh ludus -- ludus range list` (as
  root; set `LUDUS_API_KEY` for your user). From the host, use
  `LUDUS_URL`/`LUDUS_API_KEY` with your own client, or the REST API with header
  `X-API-KEY`.
- `stateful_os`: it is never re-imaged, so update it in place. Backups copy
  its OS and data disks offline (the VM is stopped meanwhile).

## netbird

- Machine `netbird`: NetBird's self-hosted stack (combined management, signal,
  relay and STUN server, plus the dashboard) behind Traefik, run with Docker
  Compose as upstream's quickstart does. Dashboard and API are at
  `https://netbird.hitkit.internal` (hitkit CA); STUN is on UDP 3478. The first
  boot pulls the container images, which needs internet access.
- hitkit claims the fresh instance: an owner login for the dashboard, an owner
  API token that `hitkit up netbird` renews before it expires (NetBird caps
  tokens at 365 days), and a reusable, non-expiring setup key named `hitkit`.
- `creds` keys: `url`, `email`, `password`, `api_url`, `api_token`,
  `api_header` (`Authorization: Token …`), `api_token_expires`, `setup_key`,
  `peer_join` and `ca_certificate`.
- `hitkit token create netbird --name NAME [--scope ROLE]` creates a service
  user `NAME` with that role (default `admin`; also `user`, `auditor`,
  `network_admin`, `billing_admin`) and prints its token once (`--json`:
  `{"name", "token", "scopes", "user", "expires"}`). Tokens last 365 days.
  `token list` shows them; `token revoke netbird NAME` deletes the service
  user. Service users get 403 on `/api/users/current`; other endpoints follow
  their role.
- Peers: `netbird up --management-url https://netbird.hitkit.internal --setup-key <setup_key>`.
  They must trust the hitkit CA: hitkit VMs do; on the host, run `sudo hitkit trust install`.
- Backups are offline (the VM stops during the copy) and include NetBird's
  SQLite stores and the cached container images on `/srv/data`.
- API example (header on stdin, out of argv):

  ```sh
  hitkit creds netbird --json | jq -r '"header = \"" + .api_header + "\""' \
    | curl -sS --cacert ~/.local/share/hitkit/ca/ca.crt -K - https://netbird.hitkit.internal/api/peers
  ```

## hitkit-dev, hitkit-dev-debian, hitkit-dev-fedora

- Machines `dev` (Arch), `dev-debian` and `dev-fedora`. Each is for
  developing hitkit itself, with nested KVM, a `dev` login user (uid 1000) with
  sudo, and a nested hitkit network of `10.78.0.0/24`.
- Access: `hitkit ssh -u dev dev`.
- Code access: the VM holds a token for the forge account `hitkit-dev`, which
  can only see repositories it has been granted. From the host:
  `hitkit grant <owner>/<repo> hitkit-dev` (default `write`), then in the VM
  `git clone https://forgejo.hitkit.internal/<owner>/<repo>.git`.
  The VM has no access to anything else on the forge.
