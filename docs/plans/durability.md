# Plan: data durability, sizing, ludus, kits guide

Status: **done** (2026-10-12). Implemented as planned, plus what the live runs
turned up; see design §13.13 (durability), §13.14 (ludus) and docs/kits.md.

Problem: a kit's data disk (`vms/<machine>/data.qcow2`, mounted at `/srv/data`)
can be deleted without anyone asking for it. That happens when the libvirt
domain disappears or the install ID changes. Separately, hitkit has no
backups at all.

## 1. Never delete a data disk implicitly

Today, three code paths `rmtree` a machine directory, data disk included, when
no *owned* domain claims it:
- `lifecycle._create`, on a "leftover from an interrupted create". This path
  is reached by `hitkit up` whenever the domain is missing.
- `lifecycle.destroy`, when the domain is missing.
- `gc --yes`, through `orphan_dirs`. That function also counts the directories
  of *indeterminate* domains (a different install ID) as orphans.

Change:
- **`preserve_data_disk(ctx, name, reason)`.** Moves `data.qcow2` to
  `vms/.kept/<name>-data.qcow2`. If that file already exists, it uses
  `<name>-data-<UTC timestamp>.qcow2` instead; it never overwrites. It writes
  a trace warning and a progress line.
- **`_create`.** Preserve the disk before clearing a leftover directory. The
  existing kept-disk reuse then brings an orphaned machine back *with its
  data* on the next `up`. Say so loudly in the output.
- **`destroy` with no domain.** Follows the same data rules as with a domain
  (see item 4).
- **`gc`.** Directories of indeterminate or foreign domains are never
  orphans. Orphan directories that hold a data disk get it preserved (moved to
  `.kept/`), never deleted. Deleting kept disks needs an explicit
  `gc --delete-data --yes`.
- **Visibility.** `status` and `gc` list kept disks (machine, size, age), so
  preserved data is visible rather than forgotten.

## 2. Recover the install ID instead of minting a new one

Today the install ID exists only in `~/.config/hitkit/config.toml` and in
each domain's metadata. If config.toml is lost, `setup` mints a new ID, and
every machine becomes indeterminate.

Change:
- **A second copy.** `config.save` also writes the ID to
  `~/.local/share/hitkit/install-id`.
- **Recovery order.** `config.load_or_create` tries, in order:
  1. config.toml
  2. the data-dir copy
  3. the hitkit metadata in the user's persisted session domain definitions
     (`~/.config/libvirt/qemu/*.xml`). These are read as files, which works
     under sudo, where root cannot reach the user's `qemu:///session`.
  
  It mints a new ID only when none of these exist. If domains disagree, it
  raises `Conflict`, listing the IDs and how many machines each owns, and
  hints at `hitkit setup --install-id`.
- **`doctor` check `install-id`.** Warns when domains carry an ID other than
  the configured one, and names the machines.

## 3. `hitkit backup` / `hitkit restore`

- **`hitkit backup <kit> [--dest DIR] [--keep N]`.** The default destination
  is `~/.local/share/hitkit/backups/<kit>/`; the docs recommend another disk.
  The default `--keep` is 7. Each backup is a directory
  `<UTC timestamp>/` containing:
  - `data.tar.zst`: the kit's data, made consistent (see below)
  - `secrets.tar`: 0600, `secrets/<kit>/` (Forgejo's SECRET_KEY and similar)
  - `manifest.json`: kit, machines, image IDs, versions, sizes, sha256
- **A `backup` hook per kit,** with a generic fallback:
  - **forge:** stop `forgejo.service` in the VM, stream
    `tar -C /srv/data forgejo | zstd` over SSH to the host, then start it
    again. The forge is down for seconds to a minute; runners and Renovate
    just retry. This is chosen over `forgejo dump` because the restore is
    trivial and exact.
  - **generic (devbox, cache, proxmox, dev kits):** a crash-consistent copy of
    `/srv/data` taken with the machine stopped. Use `--online` to tar the live
    filesystem instead.
- **`hitkit restore <kit> <backup-dir> --yes`.** Verifies the checksums,
  requires the kit to be up, stops the service, replaces the data, restores
  the secrets (`SECRET_KEY` must match what the data was encrypted with),
  restarts, and re-runs the kit's wiring (`up`).
- **`hitkit backup ls [<kit>]`** lists backups.
- **Opt-in schedule:** `hitkit backup <kit> --schedule daily|off` installs a
  user systemd timer, `hitkit-backup@<kit>.timer`, the same pattern as
  `hitkit-pool.service`.
- **E2E test:** push a commit and create an issue, back up, destroy with
  `--delete-data`, `up`, restore, then assert the commit and issue are back
  and existing mirror credentials still decrypt.

## 4. `destroy` keeps data unless told otherwise

- **New default.** `hitkit destroy <kit> --yes` keeps the data disks (moved to
  `.kept/`) and the secrets. Deleting them needs `--delete-data`.
  `--keep-data` stays accepted (it is the default now).
- **If the kit has backups,** `destroy --delete-data` prints the age of the
  newest backup, or a warning if there is none.
- **Design doc.** A new "Durability" section: what lives where, which
  commands can delete data (only `destroy --delete-data` and
  `gc --delete-data`), backups, and the mirror recommendation.

## Also update

- The skills `hitkit` and `hitkit-troubleshoot`: the destroy semantics,
  backup and restore, and kept disks.
- README usage.
- Unit tests for every path above. The orphan scenarios in particular:
  domain undefined then `up`; install ID changed then `gc --yes`;
  `destroy` without a domain.

## 5. Sizing: a data-disk knob, and making sizing stick

Requested: let users choose data-disk sizes, for example a Proxmox server
with more than 100 GiB.

A bug found along the way: `--cpus` and `--memory` aren't persisted and are
ignored for machines that already exist. The next credential-triggered
`redefine` then rebuilds the domain from the kit defaults, silently undoing a
custom size.

Change:
- **`hitkit up <kit>` takes `--cpus`, `--memory` and `--disk`.** Each accepts
  either `N` (every machine in the kit) or `MACHINE=N`, and is repeatable.
  For example: `hitkit up proxmox --disk 500`, or
  `hitkit up forge --disk runner=200 --memory forgejo=4096`. Scaled machines
  match by prefix (`runner` covers runner-1..N).
- **Persistence.** Sizing is stored per machine (in the kit's state next to
  `knobs.json`) and applied to the spec on every `up`, `upgrade` and pool
  start. A later plain `hitkit up` keeps it.
- **Existing machines:**
  - CPU and memory changes need a domain redefinition, which applies at the
    next boot. `up` says so; it does not restart the machine unannounced.
  - The data disk is **grow-only**. Grow it live with
    `virDomainBlockResize`, then run `resize2fs /dev/disk/by-id/virtio-hitkit-data`
    over SSH (ext4 grows online). Add `x-systemd.growfs` to the fstab entry as
    a backstop at boot.
  - **Shrinking is refused** with an explanation, because it would destroy
    data.
- **Thin provisioning.** qcow2 allocates lazily, so the size is a cap.
  `doctor`'s resources check warns when the sum of the data-disk caps exceeds
  the free space on the host filesystem.
- **`status` shows each machine's CPUs, memory and disk size** (cap and used).
- **The OS disk stays fixed at 10 GiB.** It's disposable by design; anything
  big belongs on `/srv/data`. Proxmox's `data` storage already lives there.

## Then: `docs/kits.md`, an authoring guide (queued after items 1–5)

Approved. Write it after items 1–5, so it covers the new `backup`
hook and the data rules. Contents:
- **What a kit is:** the knob rule, and that kits are in-tree only.
- **The four pieces:** `kit.toml` (every field), the address slot (and the
  `sudo hitkit setup` re-run for DNS), the image role, and `hooks.py`.
- **Image role conventions:**
  - `Include=../base`
  - presets, not `systemctl enable`
  - `provision.d` (runs every boot, idempotent, reads credentials)
  - `ready.d` checks, and how they become `HITKIT-FAILED gate=…`
  - `ownership.d`
  - prepare (network) versus postinst (no network)
  - `pins.toml` (URL plus sha256, at least two weeks old)
  - standalone (non-Debian) roles
- **Data rules:** everything that must survive `upgrade`, `backup` and
  `restore` lives under `/srv/data`, and the OS disk is disposable.
- **Credentials:** names at most 28 characters, `secrets.get_or_create`,
  secrets via stdin and never argv.
- **The hooks API:** `credentials`, `after_ready`, `finish`, `creds`,
  `backup`/`restore`, `before_down`/`destroyed`; `remote.run`; and logging
  through the progress callback and the trace.
- **The development loop:** `hitkit image build <role>`,
  `hitkit up <kit> --debug`, `hitkit logs`, `hitkit trace`,
  `hitkit upgrade`; and the tests to add.
- **A complete worked example** (the `postgres` sketch from the conversation).

Not in scope: a kit-authoring skill, out-of-tree kits, slots declared in
`kit.toml`. Custom-kit accessibility is deliberately not a priority yet. The
priority is "just works" with minimal risk of data loss.
