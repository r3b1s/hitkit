---
name: hitkit-troubleshoot
description: Diagnose and fix failures of the hitkit CLI and its VMs - read traces (`hitkit trace`), run `hitkit doctor`, inspect serial console logs and readiness gates, and recover from image build, boot, network, credential and ownership errors. Use when a hitkit command fails or prints `trace: <id>`, a machine never becomes ready, *.hitkit.internal is unreachable, or CI on the hitkit forge is stuck.
compatibility: Linux host running hitkit. Some fixes need the user to run sudo.
---

# Troubleshooting hitkit

Start from evidence, not guesses. Every invocation is traced, and every error
prints a trace id:

```
error: guest reported failure at gate 'forgejo': forgejo not healthy over https
hint: see the full boot log: ~/.local/share/hitkit/vms/forgejo/console.log
trace: 3f9c2a1b7e40 (details: hitkit trace 3f9c2a1b7e40)
```

## 1. Read the trace

```sh
hitkit trace                       # recent invocations: time, id, ok/failed, duration, command
hitkit trace --failed last         # every record of the latest failure
hitkit trace 3f9c --json           # raw records (id prefix is fine)
```

What the records contain:
- `command started` / `command finished` / `command failed`: argv, version,
  exit code, error, hint and the Python traceback (`exc`).
- `progress`: the human-visible progress messages.
- `span`: timed steps such as `machine.up`, `machine.create`,
  `machine.upgrade`, `image.build` and `hooks.after_ready`. A `… failed` record
  names the step and the error.
- `proc`: every subprocess (mkosi, qemu-img, ssh, systemctl, nft and others),
  with argv, exit code, duration, and the stdout/stderr tail on failure.
- `forgejo`: every API call hitkit itself makes, with method, path, status,
  duration, and the response body on errors. NetBird API calls are `netbird api`
  records (method, path, status, duration; `auth` is true when hitkit's token
  was sent). `auth` names the credential kind
  (`token` for hitkit's internal forge admin token, `basic` for the human's
  login), never its value.
- `machine.readiness`: `machine not ready`, with `gate` and the last 60
  console lines (`console_tail`).
- `virt`: libvirt connections and failures. `machine.lifecycle`: the domain
  XML as defined.

Secrets are masked as `***` in records. The raw files are
`~/.local/state/hitkit/logs/YYYY-MM-DD.jsonl`, kept 14 days. To watch a
command live, run it with `--debug` (or `HITKIT_DEBUG=1`).

## 2. Check the host

```sh
hitkit doctor --json      # exit 4 if any check fails; each check has a message and a hint
```

`doctor` covers KVM, nested virtualization, libvirt session, the `hitkit`
network, the bridge helper, /etc/hosts, subnet collisions, the firewall
(ufw/firewalld/nftables), build tools (mkosi version), CA trust and resources.
A failing host check usually needs `sudo hitkit setup`; ask the user to run
it, because it changes host networking.

## 3. Machines that don't become ready

Each guest runs readiness gates in order, then prints `HITKIT-READY` or
`HITKIT-FAILED gate=<name> detail=<text>` on its serial console.

| Gate | Checks | Typical cause |
|---|---|---|
| `credentials` | first-boot provisioning succeeded | a bad credential; look for `hitkit-provision` in the console |
| `network` | the address is configured | host network missing or down (`doctor`) |
| `dns` | `*.hitkit.internal` resolves | dnsmasq or network issue; `sudo hitkit setup` repairs it |
| `data-disk` | `/srv/data` is mounted | data disk problem |
| `ssh` | sshd is up | |
| `kvm` (dev kits) | `/dev/kvm` in the guest | nested virtualization is off on the host (`doctor`) |
| `forgejo`, `forgejo-ssh` | Forgejo is healthy over HTTPS, and SSH on port 2222 | bootstrap failed; console tail |
| `docker`, `runner` | Docker is up and the runner is registered | Forgejo unreachable from the runner |
| `caches`, `proxmox`, `pbs` | the kit's services (for `pbs`: proxmox-backup and proxmox-backup-proxy answer on 8007) | console tail |
| `ludus` | the first-boot install finished and the API answers | install failed; see the `ludus` row in section 4 |
| `netbird` | NetBird's server and dashboard answer over HTTPS through Traefik | image pull failed (no internet) or a container exits; `hitkit ssh netbird -- journalctl -u netbird -b`, then `docker compose -f /usr/lib/hitkit/netbird/compose.yml ps` |

```sh
hitkit logs <machine>                 # the whole serial console log (boot, gates, marker)
hitkit ssh <machine> -- journalctl -b -u hitkit-provision -u hitkit-ready --no-pager
hitkit ssh <machine> -- systemctl --failed
```

If the guest is reachable, `journalctl` in the guest is the richest source.
A failed `upgrade` rolls back automatically and keeps the new image's boot log
at `~/.local/share/hitkit/vms/<machine>/console.failed.log`.

## 4. Common errors

| Symptom | Meaning and fix |
|---|---|
| "hitkit is not set up on this host" | Ask the user to run `sudo hitkit setup`. |
| `cannot connect to libvirt at qemu:///session` | libvirt sockets are off: follow the hint. In a sandboxed agent shell, the sandbox may block the libvirt socket and `/run/user/<uid>`; run hitkit outside the sandbox. |
| `mkosi build failed for role …` | The `proc` record has the stderr tail, and the hint names the full log at `~/.cache/hitkit/build/<role>.mkosi.log`. `mkosi 26` is rejected; follow the hint. Network errors usually clear on a retry. |
| exit 5 / `was not created by hitkit` / `belongs to another hitkit install` | An ownership conflict. hitkit never touches foreign domains. Ask the user before passing `--force`. |
| `Forgejo API error 401/403` | Wrong or revoked token, or a missing scope. Mint a new token with the needed `--scope`. |
| `Forgejo API error 404` | Wrong owner/repo, or the account can't see it (`hitkit grant`). |
| `cannot reach Forgejo` | `hitkit status forge`, then `hitkit up forge` (idempotent; restarts what's down). |
| `internal error: <Exception>` | A hitkit bug. Report it with the output of `hitkit trace <id>` (secrets are already masked). |
| CI job stuck in `waiting` | No runner serves its `runs-on` label: `ephemeral` needs `--ephemeral N` and `hitkit pool status` showing `service_active: true`. Otherwise check `runner-N` with `hitkit status forge`. |
| Name doesn't resolve on the host | `hitkit doctor` (hosts-file and network checks). A new kit's machine (e.g. `ludus`, `netbird`) needs `sudo hitkit setup` once to publish its name. |
| `cannot reach NetBird at https://netbird.hitkit.internal: … Name or service not known` | The machine is up but the host can't resolve its name: ask the user to run `sudo hitkit setup`, then re-run `hitkit up netbird`. |
| `NetBird API error 401/403` | With your own token: revoked or expired, or its service user's role lacks the permission (service users also get 403 on `/api/users/current`). Mint another with `hitkit token create netbird`. hitkit's own token is checked and renewed by `hitkit up netbird`. |
| `NetBird is set up, but hitkit holds no valid API token` | hitkit's owner token expired (a year without `hitkit up netbird`) or the instance was claimed outside hitkit. Follow the hint; starting over deletes NetBird's data, so ask the user first. |
| Every VM stopped at once | Check `hitkit doctor`'s `session-daemon` line. VMs belong to libvirt's session daemon and live and die with its cgroup. hitkit runs it as `hitkit-libvirt.service`; if a client such as virsh spawned one elsewhere, follow the hint. |
| "resuming … from the state libvirt saved" | Expected after a logout or reboot: libvirt saved the running VM, and `up` resumes it, keeping unflushed writes. Readiness is then checked over SSH. |
| Data seems "reset" after `up` | Look at kept disks: `hitkit gc` lists `vms/.kept/`. The newest one per machine is reused, and older ones stay for inspection. |
| `refusing to shrink` | Data disks only grow. Pass a `--disk` value at least the current size. |
| `ludus` takes very long or fails on first boot | It installs Ludus from the internet: `hitkit logs ludus -f`, then `hitkit ssh ludus -- tail /opt/ludus/install/install.log`. Re-run with `hitkit ssh ludus -- systemctl restart hitkit-ludus-install`. |

## 5. Recovery moves (least to most invasive)

1. Re-run the same `hitkit up <kit>`. It is idempotent and repairs partial
   state.
2. `hitkit down <kit>`, then `hitkit up <kit>`.
3. `hitkit upgrade <kit>` if `status` shows the machine as outdated.
4. `hitkit destroy <kit> --yes`, then `hitkit up <kit>`. This recreates the
   machines and reuses the kept data disks and secrets (and, for `proxmox`, `pbs` or
   `ludus`, the OS disk). **Ask the user first.**
5. `hitkit restore <kit> --yes` from the newest backup (`hitkit backup --list`).
   Current data is moved aside, not deleted. **Ask the user first.**
6. `hitkit destroy <kit> --delete-data --yes`, which deletes the data. **Only
   with the user's explicit approval, and after checking for a backup.**

When you report a problem to the user, include the command, the error, the
hint, the trace id, and the relevant lines from `hitkit trace <id>`.
