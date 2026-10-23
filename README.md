# hitkit

**Home infrastructure toolkit**: an opinionated CLI that provisions self-hosted
development infrastructure (a Forgejo forge, CI runners, Renovate, …) as
libvirt VMs on your own Linux workstation, with every credential generated and
brokered for you.

Status: MVP plus the first post-MVP phases: a forge with CI (persistent and
single-use job VMs), Renovate, pull-through caches, Proxmox VE and Backup Server, push mirrors, git
over SSH and in-place upgrades. Supported hosts: Arch/CachyOS, Debian 13 and
Fedora 44. See [docs/design.md](docs/design.md).

## Requirements

An x86-64 Linux host with KVM. (Tested against Arch, Debian 13, and Fedora 44).
`hitkit doctor` prints the exact packages for your distribution, e.g.:

```sh
sudo pacman -S --needed libvirt qemu-base edk2-ovmf dnsmasq mkosi systemd-ukify apt debian-archive-keyring dosfstools mtools e2fsprogs
sudo apt install libvirt-daemon-system qemu-system-x86 qemu-utils ovmf dnsmasq-base mkosi systemd-ukify python3-libvirt dosfstools mtools e2fsprogs
sudo dnf install libvirt-daemon-kvm libvirt-daemon-config-network qemu-kvm edk2-ovmf mkosi systemd-ukify distribution-gpg-keys apt debian-keyring python3-libvirt dosfstools mtools e2fsprogs
```

mkosi 26 (Fedora 44's package) cannot build Debian images; install 27.1 into
hitkit's venv (`.venv/bin/pip install 'git+https://github.com/systemd/mkosi@v27.1'`) and hitkit uses it.

## Install (from a checkout)

```sh
git clone https://github.com/r3b1s/hitkit.git && cd hitkit
uv sync                 # builds libvirt-python against your system libvirt
uv run hitkit doctor
```

## Usage

```sh
sudo .venv/bin/hitkit setup        # once: network, DNS, bridge permission, ufw rules
hitkit kits                        # what can be provisioned
hitkit up devbox                   # Debian VM with SSH, ready in seconds once the image is cached
hitkit ssh devbox                  # pinned host key, no trust-on-first-use
hitkit status                      # every command accepts --json
hitkit down devbox                 # stop, keep everything
hitkit destroy devbox --yes        # remove the VM; its data disk and secrets are kept for the next `up`
hitkit destroy devbox --delete-data --yes   # also delete the data (prints the age of the newest backup)
hitkit gc --yes                    # remove orphaned directories (keeping data disks) and superseded images
hitkit up proxmox --disk 500       # sizing: --cpus/--memory/--disk N or MACHINE=N; remembered; disks grow live
```

VMs run under libvirt's per-user daemon, which hitkit starts as the user unit
`hitkit-libvirt.service`. Stopping it, for example at logout, shuts VMs down
cleanly; `loginctl enable-linger` keeps them running after logout.

Data is never deleted unless you ask for it: data disks of removed or orphaned
machines are kept in `~/.local/share/hitkit/vms/.kept/` and reused by the next
`up`. Backups:

```sh
hitkit backup forge                # forge: copied live with Forgejo stopped for seconds; others: VM stopped for an exact copy
hitkit backup forge --dest /mnt/backup --keep 14 --schedule daily   # remembered; daily user timer
hitkit backup --list
hitkit restore forge --yes         # newest backup; current data is moved aside, never deleted
```

Every invocation is traced to `~/.local/state/hitkit/logs/` (JSON lines, secrets
masked, kept 14 days). Errors print a trace id:

```sh
hitkit trace                       # recent invocations, ok/failed, duration
hitkit trace --failed last         # every record of the latest failure, with traceback
hitkit --debug up forge            # also stream the records to stderr
```

## Agent skills

hitkit ships [Agent Skills](https://agentskills.io): plain `SKILL.md` folders
that agent harnesses supporting the format load on demand.
- `hitkit`: core usage and the safety rules.
- `hitkit-forge`: tokens, repositories, git, CI and mirrors.
- `hitkit-troubleshoot`: traces, doctor and readiness gates.

```sh
hitkit skills                                  # list them (they live in skills/, also installable with `npx skills`)
hitkit skills print hitkit                      # show one skill's SKILL.md
hitkit skills install                          # into ./.agents/skills (asks before creating ./.agents)
hitkit skills install ~/.claude/skills          # or whichever skills directory your harness reads
```

Re-running `install` refreshes the copies.

### A local forge with CI and Renovate

```sh
sudo .venv/bin/hitkit trust install   # optional: trust the (name-constrained) hitkit CA on the host
hitkit up forge                    # Forgejo + 1 runner VM + Renovate; knobs: --runners N, --no-renovate
hitkit creds forge                 # URL, your username and password
hitkit token create forge --name my-agent --scope write:repository   # scoped token for an agent
git clone https://forgejo.hitkit.internal/<you>/<repo>.git
```

Workflows use `runs-on: ubuntu-latest`. Jobs run in containers on the runner VM
and trust the hitkit CA automatically, so `actions/checkout` and `actions/cache`
work over verified HTTPS. To have Renovate maintain a repository:
`hitkit grant <owner/repo> renovate-bot`.

```sh
git clone forgejo.hitkit.internal:<you>/<repo>.git      # git over SSH, pinned host key (up configures ~/.ssh/config)
hitkit up forge --ephemeral 2      # runs-on: ephemeral -> a fresh VM per job (--runners 0 sends every job there)
hitkit up forge --ephemeral 2 --arch   # runs-on: arch -> a fresh Arch Linux VM per job, the job runs on the VM itself
hitkit up cache                    # Docker Hub/ghcr/PyPI/npm/apt caches; CI uses them automatically
hitkit mirror add <you>/<repo> https://github.com/<you>/<repo>.git   # token via $HITKIT_MIRROR_TOKEN or stdin
hitkit upgrade forge               # move VMs onto newer images, keeping data (rolls back on failure)
```

Bake extra packages into the Arch runner with
`~/.config/hitkit/images/runner-arch.toml` containing `packages = ["rust", "go"]`,
then run `hitkit up forge`.

### Proxmox VE

```sh
hitkit up proxmox                  # Proxmox VE 9, nested KVM, data disk as `data` storage
hitkit creds proxmox               # web UI URL, root@pam password, API token and header
```

### Proxmox Backup Server

```sh
hitkit up pbs                      # Proxmox Backup Server 4, datastore `data` on the data disk
hitkit creds pbs                   # web UI URL, root@pam password, API token, PBS_REPOSITORY
hitkit up pbs --proxmox            # also add it to the proxmox kit's Proxmox VE as backup storage `pbs`
```

Proxmox, Proxmox Backup Server and Ludus keep system state on their OS disk, so `hitkit upgrade` leaves them
alone (update them in place with apt); backups include the OS disk.

### Ludus cyber ranges

```sh
hitkit up ludus                    # Proxmox VE 9 + Ludus 2.3.8 (8 vCPU, 32 GiB); first boot installs Ludus (needs internet, many minutes)
hitkit logs ludus -f               # watch the install
hitkit creds ludus                 # LUDUS_URL, your admin API key, Proxmox UI and root password
hitkit ssh ludus -- ludus range list   # the Ludus client is installed in the VM
```

### NetBird

```sh
hitkit up netbird                  # NetBird management server + dashboard behind Traefik; first boot pulls images
hitkit creds netbird               # dashboard login, API token, reusable setup key
hitkit token create netbird --name my-agent --scope user   # a service user with its own token
```

Peers join with `netbird up --management-url https://netbird.hitkit.internal --setup-key <key>`
and must trust the hitkit CA (`sudo hitkit trust install` on the host).

### Developing hitkit inside hitkit

```sh
hitkit up hitkit-dev               # Arch VM (also: hitkit-dev-debian, hitkit-dev-fedora)
hitkit grant <you>/hitkit hitkit-dev   # the VM's own forge account; nothing else is shared
hitkit ssh -u dev dev              # then: git clone https://forgejo.hitkit.internal/<you>/hitkit.git
```

Nested hitkit inside the dev VMs is preconfigured on `10.78.0.0/24`.

## Tests

```sh
uv run pytest                              # unit tests
HITKIT_E2E=1 uv run pytest tests/e2e -v    # MVP exit criteria against a live `hitkit up forge`
```

## Bugs, contributing and security

- Bugs and questions: [GitHub issues](https://github.com/r3b1s/hitkit/issues); see
  [CONTRIBUTING.md](CONTRIBUTING.md) for what a good report contains.
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).
- Vulnerabilities: report privately, never in an issue; see [SECURITY.md](SECURITY.md).

The canonical repository is <https://github.com/r3b1s/hitkit>.

## License

[Apache License 2.0](LICENSE).
