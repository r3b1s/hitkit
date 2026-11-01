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

## incus-os

- Machine `incus-os`: IncusOS appliance VM with 4 vCPU, 8 GiB RAM, 150 GiB data disk (`data.qcow2`),
  virtio-scsi disk, swtpm emulator 2.0 backend, and Secure Boot. It is `stateful_os` (immutable,
  self-updating OS with LUKS disk encryption sealed to TPM and NVRAM state): `upgrade` refuses,
  so updates happen in place.
- Incus remote: `hitkit-incus-os` at `https://incus-os.hitkit.internal:8443`.
- No interactive shell or SSH daemon (`hitkit ssh` refuses). Administration is done via
  the `incus` CLI, `incus admin os` against the remote, or the Incus web UI.
- Web UI: `https://incus-os.hitkit.internal:8443/ui/`. Login uses a PKCS#12 client certificate
  `browser.p12`: `hitkit browser-cert incus-os` imports it into every Firefox profile and
  `~/.pki/nssdb` (Chromium) without root (`--remove` takes it out again; `--db DIR` targets one
  database). The browsers need a restart, and accept the self-signed server certificate once.
- `creds` keys: `url`, `ui`, `remote_add` (while CLI token is pending), `server_fingerprint`,
  `ui_certificate` (path to `browser.p12`), `ui_certificate_password`, `ui_import`,
  `encryption_recovery_key`, `pool_recovery_key`, `shell`.
- Tokens: `hitkit token create incus-os --name NAME` creates an Incus trust token
  (`--json`: `{"name", "token", "remote_add", "user", "scopes", "expires"}`).
  `hitkit token list incus-os` lists pending tokens and the clients that used one (not hitkit's own); `hitkit token revoke incus-os NAME`
  revokes them.
- Durability: NVRAM and TPM emulator state are kept in `<name>-data.state/` across `destroy`
  and restored on next `up`. Backups copy disk and state offline in `<name>.data.state.tar`.

## incus-cluster

- Machines `incus-cluster-1` to `-3` (`--nodes N`, 1 to 3, default 1): IncusOS on the `incus-os`
  role, 6 vCPU, 12 GiB RAM, 200 GiB data disk. `stateful_os`, no shell, as for `incus-os`.
- Operations Center (OC) forms and manages the cluster, and runs as the Incus VM
  `operations-center` on `incus-cluster-1`. It is not a libvirt machine, so it is not in `status`.
  Its web UI is `https://operations-center.hitkit.internal:8443`, with the same browser certificate.
- `up` takes about 20 minutes, most of it the nested install and first boots.
- Needs `sudo hitkit setup` once for the names `incus-cluster-N` and `operations-center`.
- Incus remote: `hitkit-incus-cluster` at `https://incus-cluster-1.hitkit.internal:8443`.
- `creds` keys: `url`, `ui`, `server_fingerprint`, `operations_center_url`, `remote_add` (while the
  CLI token is pending), `ui_certificate`, `ui_certificate_password`, `ui_import`, `shell`, and
  `<name>_encryption_key` and `<name>_pool_key` for each node and for `operations_center`.
- Tokens: as for `incus-os`. OC's client certificate and the members' server certificates are
  not listed and cannot be revoked.
- Members can be added with a larger `--nodes`. Removing them is refused. A cluster whose setup
  failed cannot be retried: `hitkit destroy incus-cluster --delete-data` starts over.
- Updates run through OC, not on a node's own schedule.

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
