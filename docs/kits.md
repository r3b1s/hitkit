# Authoring a kit

This guide is for a developer adding a kit to the hitkit source tree. A kit is a
curated set of machines that `hitkit up <kit>` creates, wires together and waits on.
Paths are relative to the repository root. The behavior described is what the code
in `src/hitkit/` does; background is in `docs/design.md`.

## 1. What a kit is

A kit is a directory `src/hitkit/kits/<name>/` with:

- `kit.toml` (required): the kit's name, its machines, and optional knobs.
- `hooks.py` (optional): a `KitHooks` class for host-side wiring. It generates
  credentials, configures machines over SSH after they boot, and supplies `hitkit creds`.

Each machine names an image role (`src/hitkit/images/<role>/`) and an address slot
(`src/hitkit/addressing.py`).

Knobs are options passed to `up`, such as `hitkit up forge --runners 2 --no-renovate`.
**Knobs may change sizing and toggle features; they may never change topology.** A
count knob such as `runners` changes how many identical machines exist, not which
machines the kit has.

**Kits ship in-tree only.** `kits.all_kits()` globs `src/hitkit/kits/*/kit.toml`, and
`kits.load()` and `kits.hooks()` look only inside the package.

## 2. The pieces at a glance

| Piece | Location | Required | Defines |
|---|---|---|---|
| `kit.toml` | `src/hitkit/kits/<kit>/kit.toml` | yes | name, description, version, machines, knobs |
| Address slot | `_slots()` in `src/hitkit/addressing.py` | yes, one per machine name | static IP, DNS name, MAC |
| Image role | `src/hitkit/images/<role>/` | yes, one per role used | the guest image and its provisioning |
| `hooks.py` | `src/hitkit/kits/<kit>/hooks.py`, class `KitHooks` | no | credentials, post-boot wiring, `creds`, backup plan |

Without `hooks.py` the kit uses the base `Hooks` class: no credentials, no wiring, and
`hitkit creds` prints only an `ssh` line.

## 3. kit.toml reference

Top-level keys:

| Key | Type | Required | Meaning |
|---|---|---|---|
| `name` | string | yes | Must equal the directory name, because `hooks()` finds `hooks.py` by it. |
| `description` | string | yes | Printed by `hitkit kits`. |
| `version` | integer | yes | Stored on each machine as `kit-version` in its domain metadata. Nothing compares it. |
| `hooks_from` | string | no | Load `hooks.py` from another kit. `hitkit-dev-debian` and `hitkit-dev-fedora` use `hitkit-dev`. |

Knobs, `[knobs.<name>]`:

| Key | Type | Required | Meaning |
|---|---|---|---|
| `type` | `"int"` or `"bool"` | yes | Integers are range-checked; booleans become flags. |
| `default` | matches `type` | yes | Used until a value is given. |
| `min`, `max` | integer | no | Enforced for `int` knobs. |
| `help` | string | no | Shown by `hitkit kits`. |

An `int` knob takes `--name N` or `--name=N`. A `bool` knob takes `--name`, `--no-name`
or `--name=true|false`. Negating an `int` knob is an error. A knob not given keeps its
value from the previous `up`, stored in `secrets/<kit>/knobs.json`.

Machines, `[[machine]]`:

| Key | Type | Required | Meaning |
|---|---|---|---|
| `name` | string | yes | Must have an address slot. For a count-knob machine, it is the prefix. |
| `role` | string | yes | Image role directory (must contain `mkosi.conf`). |
| `cpus`, `memory_mib`, `data_disk_gib` | integer | yes | Default sizing. |
| `count_knob` | string | no | Name of an `int` knob. Instances are `<name>-1` to `<name>-N`; zero creates none. |
| `stateful_os` | boolean | no, default `false` | The OS disk holds state (section 6). |
| `ready_timeout` | integer, seconds | no, default `300` | How long `up` waits for the machine to report ready. |

An excerpt from `src/hitkit/kits/forge/kit.toml`:

```toml
name = "forge"
description = "Forgejo forge, Forgejo Actions runner VMs and Renovate, wired together"
version = 1

[knobs.runners]
type = "int"
default = 1
min = 0
max = 10

[[machine]]
name = "forgejo"
role = "forgejo"
cpus = 2
memory_mib = 2048
data_disk_gib = 20

[[machine]]
name = "runner"
role = "runner"
count_knob = "runners"
cpus = 4
memory_mib = 8192
data_disk_gib = 60
```

Machines are processed in file order, with scaled machines expanded in place. For each
machine, `up` runs `credentials`, creates or starts it, waits for readiness, then runs
`after_ready`. Put a service before the machines that depend on it, as forge does with
`forgejo` before `runner`. A count knob's `max` must not exceed the slots for its prefix,
or expansion raises `KeyError`.

**Sizing flags.** `up` accepts `--cpus`, `--memory` and `--disk`, each as `[MACHINE=]N`
and repeatable. A bare value applies to every machine. `MACHINE=N` applies to one machine,
or to every instance of a count-knob machine by prefix. Bounds: 1 to 64 vCPUs, 512 to
262144 MiB, 1 to 65536 GiB. Values are remembered in `secrets/<kit>/sizing.json` and
reapplied by later `up` and `upgrade` runs:

```sh
hitkit up forge --runners 2 --disk runner=120 --memory forgejo=4096
```

## 4. Address slots

Every machine name needs a `Slot` in `_slots()` in `src/hitkit/addressing.py`:

```python
slots += [Slot("cache", 60)]
```

`MachineSpec` looks up the slot when it is built, so a missing slot fails at parse time.
The slot fixes the static address (`10.77.0.<octet>` on the default subnet), the DNS
name `<name>.hitkit.internal`, and the MAC address.

Pick an unused octet below `30`: hitkit's own addresses are `.1`-`.29`. In use: `1`
host, `2` incus-os, `10` forgejo, `11` proxmox, `12` pbs, `13` ludus, `14` devbox, `15` dev, `16`
dev-debian, `17` dev-fedora, `18` cache, `19` netbird, `20`-`29` runner-1 to runner-10.
Octets `3`-`9` are free for new kits. The rest of the subnet is not hitkit's:
`30`-`99` are static addresses for your own guests, `100`-`199` the DHCP pool, and
`200`-`254` the overflow that holds the job pool's ci-1 to ci-55 (`docs/design.md`
section 7.2).

After adding a slot, run `sudo hitkit setup`. It needs root, is idempotent, and rewrites
the DNS records on hitkit's libvirt network (applied live) and the hitkit block in
`/etc/hosts`. Running VMs are not touched. Until setup runs, the name does not resolve.

## 5. Image roles

A role is `src/hitkit/images/<role>/`, with a `mkosi.conf`, optional `pins.toml`,
optional `mkosi.prepare.chroot` and `mkosi.postinst.chroot`, and a `mkosi.extra/` tree
copied into the image. Section 10 shows a complete role.

- **Included.** `[Include]` followed by the full line `Include=../base` inherits base's
  Debian 13 configuration, packages, common tree and partition layout. Most roles do this.
  A role can include any other role in the same way: `ludus` has `Include=../proxmox`
  and inherits that role's packages, `mkosi.extra/`, postinst script and sandbox.
- **Standalone.** The role sets its own `[Distribution]` and `[Output]`
  (`RepartDirectories=../common/mkosi.repart`) and `ExtraTrees=../common/mkosi.extra`.
  `proxmox` is standalone because its kernel and network stack differ. `hitkit-dev` is
  standalone because it is Arch.

`role_dirs()` resolves the chain: `common`, each included role, then the role itself.
`ExtraTrees=../<role>/...` lines are followed the same way. The image id is `<role>-<hash>`,
where the hash covers every file (path, exec bit, content) in the chain, so editing
`common/` rebuilds every image.

### mkosi.extra conventions

- **Presets, not `systemctl enable`.** Enable units with
  `usr/lib/systemd/system-preset/NN-<role>.preset` containing `enable <unit>` lines. mkosi
  applies presets after postinst, and Arch's default policy is `disable *`. Presets can
  also `disable` units a package would leave on (cache disables `caddy-api.service`).
- **`provision.d/`** runs on every boot from `hitkit-provision.service`, before networking,
  in name order. Hooks must be idempotent and read credentials from `$CREDENTIALS_DIRECTORY`.
  Non-executable files are skipped silently. A failing hook fails the service (the script
  uses `set -eu`), which the `credentials` gate reports.
- **`ready.d/`** gates run on every boot from `hitkit-ready.service`, in name order. The
  gate name is the file name without its number and `.sh`: `50-postgres.sh` reports as
  `postgres`. A failing gate is retried about once a second until it passes or its budget
  runs out. The budget is `HITKIT_READY_TIMEOUT`, default 120 seconds; nothing in the tree
  sets it. If all gates pass, the guest prints `HITKIT-READY`. A gate that times out prints
  `HITKIT-FAILED gate=<name> detail=<output, first 300 characters>`. Both markers go to
  `/dev/ttyS0`, which `up` reads from `console.log`. The host waits up to `ready_timeout`.
- **`ownership.d/`.** Images are built unprivileged, so postinst `chown`s are lost. Each
  line reads `owner:group path [-R]` and is applied on every boot by `provision.d/05-ownership`.
- **Build hooks.** `mkosi.prepare.chroot` runs with network access; `mkosi.postinst.chroot`
  does not. Put language-ecosystem installs in prepare, pinned by version. Postinst scripts
  mask units (base masks `systemd-firstboot` and `sshd-keygen`) and create users.
  `mkosi.sandbox/` holds the apt sources and keyrings used to build the image. Those do
  not end up in the image: base (and proxmox) ship their own `etc/apt/sources.list.d/` in
  `mkosi.extra/`, so `apt` works inside machines.
- **`pins.toml`.** Each `[[artifact]]` has `name`, `version`, `url`, `sha256`, `dest`,
  `mode`, and optionally `member`. hitkit downloads the file once, caches it by sha256,
  verifies the hash (a mismatch aborts), and places it at `dest`. With `member`, the URL
  is a `.tar.gz` and that member is extracted. The file is part of the role hash, so a
  bump rebuilds the image.
- **User packages.** A user can add packages to any role without editing it:
  `~/.config/hitkit/images/<role>.toml` with `packages = [...]`, using the role
  distribution's package names. hitkit passes them to mkosi as `--package=` and
  folds the list into the image ID, so a change marks machines outdated. With no
  list, the ID is unchanged.
- **Pinning policy.** Pin the newest stable release, with no minimum age. Nothing
  enforces this; a bump is a reviewed edit.

### Long first-boot work

Ludus is the model for installs that take minutes. Its `hitkit-ludus-install.service` is a
oneshot with `ImportCredential=hitkit.*`, `ConditionPathExists=!` on a completion marker,
and `TimeoutStartSec=2h`. A drop-in on `hitkit-ready.service` orders readiness after it.
`ready.d/60-ludus.sh` checks the marker and the API. `kit.toml` sets `ready_timeout = 3600`.
Progress is visible with `hitkit logs ludus -f`.

### Installer roles

Most roles are mkosi images that boot as disposable overlays reporting `HITKIT-READY`
on the serial console. Some operating systems (such as IncusOS) cannot be built with
mkosi because they are immutable, cryptographically signed, and provide their own installer.
These are defined as installer roles:

- **`installer.toml`**: Instead of `mkosi.conf`, an installer role directory contains
  `installer.toml` with `[installer]` defining `name`, `version`, `url`, and `sha256`.
  hitkit fetches the installation raw image, caches it by sha256, converts it to qcow2,
  and attaches it as a removable installation media disk (`install.qcow2`) during initial
  install.
- **The seed hook (`hooks.seed`)**: Returns a mapping of `{section: dict}` configuring the
  installer. It is called only when a fresh install starts (never for a machine brought
  back from a kept or restored disk), so it is also where a kit forgets secrets that
  belonged to an earlier install. hitkit writes the sections as `<section>.json` files onto
  a 16 MiB FAT image labelled `SEED_DATA` (`seed.img`) attached alongside the installer.
- **The probe hook (`hooks.probe_ready`)**: Installer guests do not run hitkit's
  `ready.d/` gates, so nothing reports on the console. hitkit polls
  `hooks.probe_ready(hctx, machine)` after every boot instead; it returns `None` when the
  machine is ready, or a reason while waiting. The machine layer never imports kits: the CLI
  and backup pass it `seed` and `probe` callables (`kits.installer_hooks`), and refuse an
  installer machine without them.
- **No shell.** `hitkit ssh` refuses installer machines and points at `hitkit creds <kit>`,
  and `hitkit upgrade` refuses them: they update themselves.
- **State sidecar (`<name>-data.state/`)**: Operating systems that bind disk encryption to a
  virtual TPM and Secure Boot NVRAM (like IncusOS) require the TPM emulator state (`tpm/`)
  and `nvram.fd` to remain paired with the disk: without them the disk unlocks only with
  its recovery key. For machines with a `tpm/` directory (installer machines), keeping a
  data disk moves `nvram.fd` and `tpm/` into a `<name>-data.state/` sidecar next to it, and
  backups add a `<name>.data.state.tar`; both come back whenever the disk is reused.
  `gc --delete-data` deletes the sidecar with its disk.
- **Single disk (`data.qcow2`)**: Installer machines install the complete system onto a single
  disk labeled with serial `hitkit-data` (`data.qcow2`). There is no separate `os.qcow2`.
  `--disk` sizes this disk, and storage pools expand into the additional space on subsequent
  reboots.

## 6. Data rules

- **Persistent state lives under `/srv/data`.** Each machine's data disk is mounted there
  by the common fstab and formatted on first boot. Anything that must survive `upgrade`,
  `backup` and `restore` belongs there, for example forgejo's `/srv/data/forgejo`. The OS
  disk is an overlay on the cached image and is disposable.
- **`upgrade`** stops the machine, boots a fresh overlay on the new image, keeps the data
  disk and credentials, and waits for readiness. If the machine does not come up, the old
  overlay is restored.
- **Backups.** `hitkit backup <kit>` copies each machine's `/srv/data`, offline by default
  (stop, copy the data disk, start), or online when `backup_plan` says so. Backups go to
  `~/.local/share/hitkit/backups/<kit>/`; the newest 7 are kept. `hitkit restore` never
  deletes current data; it moves it aside first.
- **Destroy and gc keep data.** `hitkit destroy <kit> --yes` moves the data disk to
  `vms/.kept/` and keeps the secrets. The next `up` reuses the kept disk. Only
  `destroy --delete-data` and `gc --yes --delete-data` delete data.
- **`stateful_os = true`** is for OS disks that hold state which cannot move to `/srv/data`.
  Then `upgrade` refuses and points at in-place updates, `destroy` and `gc` keep the OS
  disk, backups copy it, and `status` does not mark the machine outdated. Proxmox qualifies
  because its VM definitions live in its config database. Ludus qualifies because of its
  Linux users, `/opt/ludus` and WireGuard keys. Other kits should leave it off.

## 7. Credentials and secrets

### Credentials delivered to guests

`hooks.credentials(hctx, machine, plan)` returns `{name: bytes}`. hitkit writes each value
to `vms/<machine>/creds/<name>` (mode 0600), exposes it through QEMU fw_cfg, and systemd
imports it. Units read it with `ImportCredential=` or `LoadCredential=`; scripts read
`$CREDENTIALS_DIRECTORY`.

- **Naming.** The hitkit units import `hitkit.*`, so kit credentials use
  `hitkit.<service>.<item>`, for example `hitkit.pve.root-password`.
- **Length.** A name may be at most `MAX_NAME` = **28 characters**, not counting the prefix.
  QEMU limits fw_cfg names to 55 characters, and `opt/io.systemd.credentials/` uses 27 of
  them (`src/hitkit/machine/creds.py`). A longer name raises `ValueError`.
- **Common credentials.** Every machine gets these, and a kit may not reuse their names:
  `system.hostname`, `hitkit.network` (static address), `hitkit.ssh-host-key`,
  `hitkit.authorized-keys`, `hitkit.tls.crt` and `hitkit.tls.key` (a leaf certificate for the
  machine's name), and `hitkit.ca.crt`.
- **Stability.** Each `up` compares the files on disk with what the hooks return. A changed
  or removed value restarts the machine so the new value applies. Generate values with
  `get_or_create`, never fresh on every call.

### Host-side secrets

`hitkit.secrets` stores host secrets as 0600 files in `~/.local/share/hitkit/secrets/<kit>/<name>`.

- `secrets.get_or_create(user, kit, name, generate)` is the usual call, with `generate` a
  zero-argument function. Also `get(user, kit, name)` and `put(user, kit, name, value)`.
- Generators: `secrets.password()` (URL-safe random text), `secrets.hex_secret(n)` and
  `secrets.b64url_32()`.
- Each value read or written is registered with `trace.secret` and masked in later trace
  records. Only values of at least 8 characters with no whitespace are registered. Names
  ending in `.json` or `.pub` are not masked.

### Passing secrets to machines

`remote.run(hctx, machine, command, stdin=None)` runs `command` as root over SSH with the
machine's pinned host key. It returns stdout and raises `HitkitError` on a non-zero exit.
**Send secrets on `stdin`, never in `command`.** The trace records `argv` in full and only
the size of stdin. Ludus sends its admin password as JSON on stdin; forge sends runner
secrets with `--secret-stdin -`.

## 8. Hooks API

`hooks.py` defines `class KitHooks(Hooks)`. The module loads once per process, so keep
top-level code light.

| Method | When it runs | Purpose |
|---|---|---|
| `credentials(self, hctx, machine, plan) -> dict[str, bytes]` | before each machine is created or updated (`up`, `upgrade`) | extra credentials for the machine |
| `seed(self, hctx, machine, plan) -> dict[str, dict]` | when a fresh install of an installer machine starts (`up`) | seed files for installer roles (`<section>.json` on FAT `SEED_DATA` disk) |
| `probe_ready(self, hctx, machine) -> str \| None` | polled after every boot of an installer machine (`up`, `backup`, `restore`) | `None` when ready, otherwise a reason |
| `after_ready(self, hctx, machine, plan, knobs, log)` | after each machine reports ready, in plan order, on every `up` and `upgrade` | configure the running machine; must be idempotent |
| `next_steps(self, hctx, plan) -> list[str]` | after all machines are ready in `up` | replaces generic `Next: hitkit creds … hitkit ssh …` lines |
| `browser_certificate(self, hctx) -> dict or None` | `hitkit browser-cert <kit>` | the PKCS#12 login certificate a browser uses: `{"bundle", "password", "nickname", "certificate"}` |
| `finish(self, hctx, plan, knobs, log)` | once every machine is ready (also after `upgrade`) | wiring across machines |
| `creds(self, hctx) -> dict` | `hitkit creds <kit>` (and `--json`) | URLs and logins; raise `HitkitError` with a hint if the kit is not up |
| `backup_plan(self, machine) -> BackupPlan or None` | `hitkit backup`, per existing machine | `BackupPlan()` for offline (the default); `BackupPlan("online", ("forgejo.service",))` stops the listed units around a live copy; `None` skips the machine |
| `before_down(self, hctx)` | `hitkit down <kit>`, if defined | stop kit-level services (forge drains its ephemeral pool) |
| `destroyed(self, hctx)` | `hitkit destroy <kit> --delete-data --yes`, if defined | remove host-side state; not called when data is kept |

`machine` in `backup_plan` is a `MachineInfo` (`.name`, `.meta`). `hctx` provides
`hctx.user`, `hctx.cfg`, and `hctx.paths(name)` for a machine's `known_hosts` and
`console_log`. Forge checks `hctx.paths("cache").known_hosts.exists()` to see whether the
cache kit is up. `hitkit token` needs `token_create`, `token_list` and `token_revoke`.

**Logging.** `log(msg)` prints a progress line on stderr (unless `--debug`) and records it
in the trace under `progress`. Use it for each step a user should see. For structured
records, use `_t = trace.logger(__name__)` and `_t.info("ready", machine=name)`.

**Errors.** Raise `HitkitError(message, hint=...)`. The CLI prints the message and the hint,
then exits non-zero with a trace id.

## 9. Development loop

1. **Build the role.** `hitkit image build <role>` (default `base`) builds even when a cached
   image exists. A failed mkosi build leaves its output in `~/.cache/hitkit/build/<role>.mkosi.log`.
2. **Bring the kit up with debug output.** `hitkit --debug up <kit>`. `up` builds any role
   whose current configuration has no image yet.
3. **Watch the boot.** `hitkit logs <machine> -f` follows the serial console, where gate
   results and the readiness markers appear.
4. **Read failures.** `hitkit trace --failed last` shows the latest failed run, including
   the console tail of a "machine not ready" record. `hitkit trace <id>` shows one run.
5. **Inside the guest.** `hitkit ssh <machine>` logs in as root; `hitkit ssh <machine> -- <cmd>`
   runs one command, for example `journalctl -u hitkit-provision -b --no-pager`.
6. **Iterate.**
   - A change under a role, `common/`, or an included role changes the image hash. Existing
     machines then show `(outdated)` in `hitkit status`, and `up` prints a hint. Apply with
     `hitkit upgrade <kit>`.
   - Hand edits inside a VM are not in the image and are lost on the next upgrade.
   - A `hooks.py` change needs no new image; run `hitkit up <kit>` again.
   - A `stateful_os` machine cannot be upgraded. `destroy` keeps its OS disk, so an image
     change reaches it only after `destroy --delete-data`, which also removes its data. Test
     such a role on a throwaway kit first.
   - For a clean test, `hitkit destroy <kit> --yes --delete-data` deletes the data disks and
     the kit's secrets.

**Tests to add.** Run them with `uv run pytest`, following the existing files:

- `tests/test_kits.py` and `tests/test_kit_knobs.py`: the kit parses; machines, roles, knob
  defaults and bounds are right.
- `tests/test_image_pins.py`: the chain from `images.role_dirs(role)` and the pins from
  `images.pinned_artifacts(...)`.
- Hook tests replace `remote.run` through the hook module's globals,
  `monkeypatch.setattr(hooks.after_ready.__globals__["remote"], "run", fake)`. See
  `tests/test_dev_and_proxmox_hooks.py` and `tests/test_ludus.py`. The `user` fixture in
  `tests/conftest.py` gives a home under `tmp_path`.
- Idempotence: call `credentials` twice and compare. Credential names: assert
  `len(name) <= creds.MAX_NAME`, as `tests/test_forge_hooks.py` does.
- `tests/e2e/` runs against real VMs and is skipped unless `HITKIT_E2E=1` is set.

## 10. Worked example: `postgres`

> **Example only.** This kit is not in the tree. It applies the conventions above to a
> hypothetical PostgreSQL server. The PostgreSQL details are a sketch and have not been
> built into an image.

It shows a password credential, a cluster on the data disk, a readiness gate, and a
bootstrap unit modeled on forgejo's `forgejo-bootstrap.service`.

**`src/hitkit/kits/postgres/kit.toml`**

```toml
# PostgreSQL 17 server, cluster on the data disk.
name = "postgres"
description = "PostgreSQL 17 server with its cluster on /srv/data"
version = 1

[[machine]]
name = "postgres"
role = "postgres"
cpus = 2
memory_mib = 2048
data_disk_gib = 20
```

**Address slot.** Octet 61 is unused, so in `_slots()`:

```python
slots += [Slot("postgres", 61)]
```

Then run `sudo hitkit setup`.

**`src/hitkit/images/postgres/mkosi.conf`**

```ini
# postgres: PostgreSQL 17 on hitkit's Debian 13 base.
[Include]
Include=../base

[Content]
Packages=postgresql-17
```

**`mkosi.postinst.chroot`** (executable). Debian's package creates a default cluster at
install time. Dropping it lets the cluster be created on the data disk at boot.

```sh
#!/bin/sh
set -e
pg_dropcluster --stop 17 main
```

**`mkosi.extra/usr/lib/hitkit/provision.d/50-postgres`** (executable), run on every boot:

```sh
#!/bin/sh
set -eu
install -d -o postgres -g postgres -m 0700 /srv/data/postgres
[ -f /srv/data/postgres/17/main/PG_VERSION ] || pg_createcluster --datadir=/srv/data/postgres/17/main 17 main
```

**`mkosi.extra/usr/lib/hitkit/ready.d/50-postgres.sh`** (executable), the gate `postgres`:

```sh
#!/bin/sh
pg_isready -q -h 127.0.0.1 -p 5432 || { echo "postgres not accepting connections on 5432"; exit 1; }
```

**`mkosi.extra/usr/lib/hitkit/postgres-bootstrap`** (executable), run as `postgres` after
each start. The password reaches `psql` on stdin only. Generated passwords are URL-safe,
so they need no quoting.

```sh
#!/bin/sh
set -eu
{
    printf "ALTER USER postgres PASSWORD '"
    cat "$CREDENTIALS_DIRECTORY/hitkit.pg.password"
    printf "';\n"
} | psql -q -X -v ON_ERROR_STOP=1
```

**`mkosi.extra/usr/lib/systemd/system/postgres-bootstrap.service`**

```ini
[Unit]
Description=Set the PostgreSQL superuser password from its credential
After=postgresql.service hitkit-provision.service
Requires=postgresql.service hitkit-provision.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=postgres
LoadCredential=hitkit.pg.password
ExecStart=/usr/lib/hitkit/postgres-bootstrap

[Install]
WantedBy=multi-user.target
```

**`mkosi.extra/usr/lib/systemd/system-preset/20-postgres.preset`**

```
enable postgresql.service
enable postgres-bootstrap.service
```

**`src/hitkit/kits/postgres/hooks.py`**

```python
"""Wiring for the postgres kit (example, not in the tree)."""

from __future__ import annotations

from hitkit import secrets
from hitkit.errors import HitkitError
from hitkit.kits import Hooks

KIT = "postgres"


class KitHooks(Hooks):
    def credentials(self, hctx, machine, plan):
        pw = secrets.get_or_create(hctx.user, KIT, "superuser.password", secrets.password)
        return {"hitkit.pg.password": pw.encode()}

    def creds(self, hctx) -> dict:
        pw = secrets.get(hctx.user, KIT, "superuser.password")
        if pw is None:
            raise HitkitError("the postgres kit has not been brought up yet", hint="hitkit up postgres")
        return {"superuser": "postgres", "password": pw, "shell": "hitkit ssh postgres"}
```

**`tests/test_postgres_hooks.py`**

```python
from types import SimpleNamespace

import pytest

from hitkit import kits
from hitkit.errors import HitkitError
from hitkit.machine import creds as machine_creds


@pytest.fixture
def hctx(user):
    return SimpleNamespace(user=user)


def test_kit_has_one_postgres_machine():
    assert [(m.name, m.role) for m in kits.load("postgres").machines] == [("postgres", "postgres")]


def test_password_is_generated_once_and_fits_the_limit(hctx):
    hooks = kits.hooks("postgres")
    first = hooks.credentials(hctx, None, ())
    assert set(first) == {"hitkit.pg.password"}
    assert all(len(n) <= machine_creds.MAX_NAME for n in first)
    assert hooks.credentials(hctx, None, ()) == first


def test_creds_before_bring_up_raises(hctx):
    with pytest.raises(HitkitError, match="not been brought up"):
        kits.hooks("postgres").creds(hctx)
```

**Trying it:**

```sh
sudo hitkit setup
hitkit image build postgres
hitkit --debug up postgres
hitkit creds postgres
uv run pytest tests/test_postgres_hooks.py
```
