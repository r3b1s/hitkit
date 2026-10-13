---
name: hitkit
description: Provision and operate local self-hosted dev infrastructure (VMs) with the hitkit CLI - a Forgejo forge with CI runners, pull-through caches, Proxmox VE, plain Debian devboxes, and hitkit development VMs on this Linux workstation. Use when the user mentions hitkit, *.hitkit.internal hosts, or wants a local forge, CI runner, package cache, Proxmox, or a throwaway VM; covers bringing kits up and down, status, SSH, credentials, sizing knobs, upgrades and cleanup.
compatibility: Linux host where `sudo hitkit setup` has been run (libvirt, QEMU/KVM, mkosi). Needs a shell; no network beyond the host.
---

# hitkit

hitkit provisions opinionated dev infrastructure as libvirt/QEMU-KVM VMs on
the user's own machine. You pick a **kit**; hitkit builds images, creates
machines, issues TLS certificates from its own CA, brokers every credential,
and waits until each machine reports ready. It is not infrastructure-as-code:
knobs change sizing and toggle features, never topology.

## Rules for agents

1. **Use `--json`** for anything you parse. JSON goes to stdout; progress goes
   to stderr. Never scrape the human output.
2. **Ask before anything destructive or privileged.** Confirm with the user
   before `destroy`, `restore`, `gc --yes`, `image prune --yes`, or any
   `sudo hitkit …` (`setup`, `trust`). Never pass `--delete-data` without the
   user's explicit approval. Prefer `down` (keeps everything) to `destroy`.
3. **Keep secrets out of argv, chat and files.** `hitkit creds` prints
   passwords; use the values directly and never repeat them in your replies,
   commits or logs. Pipe tokens on stdin or pass them in environment variables.
4. **Mint your own credentials.** For forge access, create a scoped token
   (`hitkit token create forge --name agent-<task>`) instead of using the
   human's password, and revoke it when the task is done (see the
   `hitkit-forge` skill).
5. **Go through hitkit.** Don't edit machines with `virsh`, delete files
   under `~/.local/share/hitkit`, or hand-edit `/etc/hosts`. libvirt metadata
   is hitkit's source of truth; out-of-band changes produce ownership
   conflicts (exit code 5).
6. **Expect long first runs.** The first `up` of a role builds its image with
   mkosi, which takes several minutes (up to about 20 minutes). Later runs take
   seconds. Use a generous command timeout (30 minutes or more) and don't
   interrupt it.
7. **On failure, read the trace before retrying.** Every error prints
   `trace: <id>`; run `hitkit trace <id>` (see `hitkit-troubleshoot`).

## First look

```sh
hitkit doctor --json          # is the host set up? exit 4 = a check failed
hitkit kits --json            # kits, machines, sizes, knobs
hitkit status --json          # machines, state, address, image, outdated flag
```

If `doctor` or any command says hitkit is not set up, stop and ask the user to
run `sudo hitkit setup`. It needs root and changes host networking.

**Sandboxed shells.** If your harness sandboxes commands, hitkit may be unable
to reach the libvirt socket (`/run/user/<uid>/libvirt`), `*.hitkit.internal`, or
its own state directories. Symptoms: `cannot connect to libvirt`, `Read-only
file system`, `doctor` failing `kvm`/`libvirt-session`/`network`, or
connection errors. Those are the sandbox, not the host. Run hitkit commands
outside the sandbox (with the user's approval, as your harness requires)
before you diagnose anything.

## Kits

| Kit | Machines | Use it for |
|---|---|---|
| `devbox` | `devbox` | A plain Debian 13 VM with SSH and a persistent `/srv/data` |
| `forge` | `forgejo`, `runner-1`… | A Forgejo forge, CI runners and Renovate (see `hitkit-forge`) |
| `cache` | `cache` | Pull-through caches for Docker Hub, ghcr.io, PyPI, npm and apt |
| `proxmox` | `proxmox` | Proxmox VE 9 with an API token, for testing Proxmox tooling |
| `hitkit-dev` (`-debian`, `-fedora`) | `dev` (`dev-debian`, `dev-fedora`) | Developing hitkit itself, with nested KVM |

Per-kit details (endpoints, credentials, knobs): [references/kits.md](references/kits.md).

## Lifecycle

```sh
hitkit up devbox                      # create or start, then wait for ready. Idempotent.
hitkit up forge --runners 2 --no-renovate   # kit knobs follow the kit name; they persist
hitkit up devbox --cpus 4 --memory 8192     # sizing for every machine in the kit
hitkit up forge --disk runner=200     # MACHINE=N targets one machine (or every runner-N); remembered
hitkit down devbox                    # graceful shutdown, keeps everything
hitkit destroy devbox --yes           # removes the VM; keeps its data disk and secrets (ask first)
hitkit destroy devbox --delete-data --yes   # also deletes the data: only with explicit approval
hitkit upgrade forge                  # move onto current images; keeps data; rolls back on failure
```

**Data safety.**
- Data disks of destroyed or orphaned machines are kept in
  `~/.local/share/hitkit/vms/.kept/` and reused by the next `up`. `status`
  and `gc` list them.
- Data disks only grow; asking for a smaller `--disk` is refused.
- `proxmox` and `ludus` are `stateful_os`: `upgrade` refuses them (update in
  place), and their OS disk is kept and backed up too.

**Backups.**

```sh
hitkit backup forge --json           # forge: live, Forgejo paused seconds; other kits: VM stopped during the copy
hitkit backup --list --json
hitkit restore forge --yes           # newest backup (ask first); current data is moved aside, not deleted
```

Before risky work on a kit's data (migrations, mass edits), take a backup.

- `up` is safe to re-run. It repairs what's missing, re-applies changed
  credentials (restarting only the affected machine), and keeps knobs from
  the previous `up` unless you pass new ones.
- `status` marks machines whose image is older than their role's current one
  (`"outdated": true`); `hitkit upgrade <kit>` fixes that. JSON rows also
  carry `cpus`, `memory_mib`, `disk_gib`, `disk_used_gib` and `stateful_os`.
- A machine that libvirt saved when the user's session ended is **resumed** by
  `up`, not booted. That is expected and keeps unflushed data.
- Machines are `<name>.hitkit.internal` on a private /24 (default
  `10.77.0.0/24`), with static addresses. The host resolves these names.

## Getting in

```sh
hitkit ssh devbox                     # root, pinned host key (no trust-on-first-use)
hitkit ssh devbox -- systemctl status # run one command
hitkit ssh -u dev dev                 # another login user
hitkit creds <kit> --json | jq 'del(.password)'   # URLs and logins; never echo the password (rule 3)
hitkit logs <machine> [-f]            # serial console (boot output, readiness markers)
```

Never disable host-key checking. hitkit pins each machine's key.

TLS: every service has a certificate from the hitkit CA. The CA file is the
`ca_certificate` path in `hitkit creds forge --json`, normally
`~/.local/share/hitkit/ca/ca.crt`. Pass it to clients (`curl --cacert`,
`REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `GIT_SSL_CAINFO`) unless the user
ran `hitkit trust install`. Never use `-k` or `verify=False`.

## Housekeeping

```sh
hitkit image ls --json                # cached images and the pinned versions in each
hitkit gc --json                      # dry run: orphaned VM dirs and unused images
hitkit gc --yes                       # actually remove them (ask first)
```

## Output and exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | OK | |
| 1 | Error | Read `error` and `hint`, then `hitkit trace <trace>` |
| 2 | Usage error | Fix the arguments (`hitkit <cmd> --help`) |
| 3 | Needs root | Ask the user to run it with sudo |
| 4 | Doctor checks failed | Show the failing checks and their hints to the user |
| 5 | Conflict | Existing state hitkit won't touch; ask the user before passing `--force` |

With `--json`, errors are `{"error": …, "hint": …, "trace": …}` on stdout.
Set `HITKIT_TRACE_ID=<your-id>` to group several invocations under one trace.
