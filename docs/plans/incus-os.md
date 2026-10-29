# Plan: the incus-os kit

Status: **ready to implement** (2026-10-29). The spike is done (section 3), and
its results are folded into the sections below. The detailed findings are in
[docs/incus/](../incus/README.md).

Goal: `hitkit up incus-os` installs [IncusOS](https://github.com/lxc/incus-os)
into a VM and finishes with an Incus server you can add as a remote to the
`incus` CLI on your workstation, with the Incus web UI usable from your browser.

## 1. Decisions

- **Install the official image; do not build IncusOS with mkosi.** hitkit pins
  the upstream install image by version and sha256 and configures it with
  IncusOS's install seed. Section 2 gives the reasons.
- **Names.** Kit `incus-os`, machine `incus-os` (`incus-os.hitkit.internal`,
  address slot `.2`), Incus remote `hitkit-incus-os`.
- **One disk.** IncusOS installs onto a single disk. Its `local` ZFS pool takes
  all the space left after the system partitions (about 34 GiB). `--disk` sizes
  that disk.
- **IncusOS's built-in updater stays on**, with its defaults: the stable
  channel, a check every 6 hours, no automatic reboot. The pin only decides
  which version gets installed. After that the machine updates itself in place,
  as proxmox and pbs do with apt. `hitkit upgrade` refuses (`stateful_os`).
- **The server stays standalone, not clustered.** A single-node cluster would let
  the server present a hitkit-CA certificate, but it changes what the API and CLI
  report (location, member-specific pool config). The kit should behave like a
  normal Incus server. hitkit pins the server's own certificate on first contact
  instead (section 6.2). See
  [docs/incus/clustered-vs-standalone.md](../incus/clustered-vs-standalone.md).
- **The web UI is enabled and usable.** hitkit generates a browser client
  certificate that the server trusts and exports it as a PKCS#12 file for you to
  import (section 7).
- **Remote setup.** hitkit mints an Incus trust token and prints
  `incus remote add hitkit-incus-os <token>` (section 6.4). The token carries
  the server's certificate fingerprint, so there is no unverified "ok (y/n)?"
  prompt.

## 2. Why not build with mkosi

- **No official updates.** IncusOS images are signed: the UKIs, the `/usr`
  verity signature, the Secure Boot keys the image enrolls, and the S/MIME-signed
  update index (`index.sjson`). A local build must use its own keys, so it could
  never take updates from images.linuxcontainers.org. We would have to run an
  update server forever.
- **Nothing for hitkit to add.** The OS is immutable and `/usr` is signed, so
  hitkit's `common/` tree (provision.d, ready.d, sshd, fstab) cannot be layered
  in without breaking the signature. A self-built image would end up identical to
  upstream, only harder to maintain.
- **The build does not fit hitkit.** `make build` runs `sudo mkosi`, needs Go
  and `debuild`, and builds the application sysexts (Incus itself). hitkit
  builds images unprivileged.
- **Pinning works upstream.** `https://images.linuxcontainers.org/os/index.json`
  lists each release with its channels and a sha256 per file. That is exactly
  what `AGENTS.md` asks for.

The newest stable release when this was written:

| | |
|---|---|
| version | `202610272235` (channels `testing`, `stable`) |
| url | `https://images.linuxcontainers.org/os/202610272235/x86_64/IncusOS_202610272235.img.gz` |
| sha256 | `86656db79efcc2cb43ca7fb02c91921b0c556d8d351fdb878741a0f733868b50` |
| size | 615156124 bytes (matches the HTTP `content-length`) |

## 3. Spike (done, 2026-10-29)

The spike was done by hand, outside hitkit, with `virsh` on `qemu:///session`
and the `hitkit0` bridge, at addresses `10.77.0.30` and `.31` (your own-guest
range, so no slot and no sudo). Host: Arch/CachyOS with libvirt 12.8. Guest:
IncusOS `202610272235` (Incus 7.5.1). Nothing found blocks the plan.

| Question | Answer |
|---|---|
| Q1. How does the host know the install finished? | The console log carries "IncusOS was successfully installed." / "Please remove the install media". Detaching the install and seed disks live makes IncusOS reboot itself into the installed system. |
| Q2. Secure Boot and TPM in session mode? | Works with no degraded mode. Firmware autoselection gives a secure-boot OVMF with empty keys, and IncusOS enrolls its own. swtpm runs as the user, with its state in a directory of our choosing. IncusOS reports "system state is fully trusted". Keeping NVRAM and TPM state across an undefine works; losing the TPM state stops the boot at a recovery-key prompt. |
| Q3. Can the server present a hitkit-CA certificate? | Only as a single-node cluster. **Decided: stay standalone** and pin on first contact. Clustering changes what reads return (location, member-specific pool config); see [clustered-vs-standalone.md](../incus/clustered-vs-standalone.md). |
| Q4. Can the disk grow? | Yes, on the next reboot: a live resize from 150 to 200 GiB grew the pool from 116 to 166 GiB after one reboot. |
| Q5. Remote and web UI? | `incus remote add <name> <token>` works with no prompt and refuses a wrong fingerprint. hitkit can build the token with only the DNS address. The `<url> --token` form still prompts, so it is not used. The UI is at `/ui/`; a PKCS#12 from `cryptography` imports into NSS. |
| Q6. Seed format? | `<section>.json` files on a FAT `SEED_DATA` disk, decoded strictly. `target.id`, `network` with `hwaddr`, and `security.custom_ca_certs` all worked. A `kernel` seed with `ttyS0` breaks startup and is not needed. |

**Timing.** From `virsh start` to a trusted API took about 1 min 40 s: key
enrollment and its reboot, a 35 s install, the media detach, then 57 s to the
API. A normal boot takes 22 s.

Not covered by the spike, to cover in implementation (section 11):
- session-end save and resume with a TPM;
- Debian 13 and Fedora 44 hosts;
- an IncusOS self-update;
- logging into the UI through a real browser window.

Details, commands and the seed files that worked:
[docs/incus/incus-os-in-libvirt.md](../incus/incus-os-in-libvirt.md).

## 4. Machine layer: installer machines

Today every role is an mkosi image that boots as an overlay and reports
`HITKIT-READY` on the serial console. IncusOS is a new kind of role.

### 4.1 Image

- `src/hitkit/images/incus-os/installer.toml` instead of `mkosi.conf`:
  ```toml
  # IncusOS install image (stable channel), from https://images.linuxcontainers.org/os/index.json
  [installer]
  name = "IncusOS"
  version = "202610272235"
  url = "https://images.linuxcontainers.org/os/202610272235/x86_64/IncusOS_202610272235.img.gz"
  sha256 = "86656db79efcc2cb43ca7fb02c91921b0c556d8d351fdb878741a0f733868b50"
  ```
- `images.role_dirs`, `config_hash`, `find` and `ensure` accept installer
  roles. `ensure` reuses `fetch_artifact` (download once, verify sha256),
  decompresses the image and converts it to `<role>-<hash>.qcow2` in the image
  cache (about 10 s; 729 MiB). The image summary records the versions as
  `{"incus-os": "<version>"}`. `gc` and `unreferenced_images` work as they do
  today.
- A pin bump is a reviewed edit, as for any pin. It only affects new installs.
  Pick the newest index entry whose `channels` include `stable`.

### 4.2 Domain

When the role is an installer role, `domain_xml` emits the XML in
[incus-os-in-libvirt.md](../incus/incus-os-in-libvirt.md) section 3:
- **Firmware:** `secure-boot` yes, `enrolled-keys` no, and `<smm state='on'/>`.
- **TPM:** `<tpm model='tpm-crb'>` with an emulator 2.0 backend,
  `persistent_state='yes'`, and `<source type='dir' path='vms/<machine>/tpm'/>`.
- **Disks:** a `virtio-scsi` controller, with the machine's disk on `bus='scsi'`
  and boot order 1. IncusOS does not detect virtio-blk disks.
- **Install media** (only while installing): `install.qcow2` (overlay on the
  cached image) with boot order 2, and `seed.img`. Both are removable USB disks
  on a `qemu-xhci` controller.
- **No graphics device.** The serial port is then the console, and
  `console.log` carries the TUI.
- **No fw_cfg credentials, and no guest agent channel.**

### 4.3 Disk and paths

- The single disk is the machine's `data.qcow2`, with serial `hitkit-data`; the
  install target is `id: hitkit-data`. That reuses the data-disk machinery as
  it is: `--disk` sizing, kept disks, backup and restore. The name is a little
  misleading here (the disk holds the whole system), so `docs/kits.md`
  explains it.
- The machine directory adds `install.qcow2`, `seed.img` (both deleted after the
  install) and `tpm/`. With `nvram.fd` and the disk, `tpm/` is the machine's
  state (section 4.5).

### 4.4 Install flow

1. Define the domain with the install media attached, and start it. The first
   boot enrolls the Secure Boot keys and reboots; the second boot installs.
2. Wait for "IncusOS was successfully installed." in `console.log`, up to
   `ready_timeout`.
3. Detach the install and seed disks (`VIR_DOMAIN_AFFECT_LIVE | _CONFIG`).
   IncusOS reboots itself. Delete `install.qcow2` and `seed.img`.
4. Wait for readiness through the kit's probe (section 6.2).

An interrupted `up` is recognised by install media still attached in the domain
XML, and the next `up` continues at step 2. If the media is detached late,
IncusOS's "already installed, please remove the media" path covers it. A machine
brought back from a kept or restored disk skips the install entirely.

### 4.5 State that must survive

IncusOS encrypts its disk with keys sealed to the TPM and measured against the
Secure Boot state, so `nvram.fd`, `tpm/` and the disk belong together. The
spike proved this: without the TPM state the boot stops at a recovery-key
prompt.
- **`_teardown`.** Today it undefines with `UNDEFINE_NVRAM`, and libvirt also
  deletes the swtpm state. For installer machines, unless `--delete-data`, it
  undefines with `UNDEFINE_KEEP_NVRAM | UNDEFINE_KEEP_TPM`. NVRAM and TPM state
  then move to `.kept/` next to the disk, as a sidecar `<name>-data.state/`.
  The next `up` brings them back with the disk.
- **Backup and restore.** These copy the sidecar together with the disk.
  Backups stay offline.
- **Recovery keys.** hitkit stores them as a fallback (section 6.3).

### 4.6 Other machine-layer changes

- **Readiness.** `_wait_ready` calls the kit's probe instead of reading the
  console marker. The probe also replaces the SSH-based resume path.
- **Sizing.** `--disk` can grow the disk: the `local` pool takes the new space
  at the next reboot. `up` grows the qcow2 and says the space arrives after a
  reboot; it does not reboot the machine by itself.
- **`hitkit ssh incus-os`.** Fails with a hint that IncusOS has no shell: use
  `incus` / `incus admin os …` against the `hitkit-incus-os` remote.
- **`hitkit logs incus-os`.** Unchanged: the serial console carries the TUI.

## 5. The kit

### 5.1 kit.toml

```toml
# IncusOS (https://linuxcontainers.org/incus-os): an immutable OS running Incus, installed
# from the official image. It updates itself in place, so its disk is stateful.
name = "incus-os"
description = "IncusOS with Incus and its web UI on https://incus-os.hitkit.internal:8443"
version = 1

[[machine]]
name = "incus-os"
role = "incus-os"
cpus = 4
memory_mib = 8192    # IncusOS wants 4 GiB for itself
data_disk_gib = 150  # the whole disk; the local pool gets about 116 GiB
stateful_os = true
ready_timeout = 600  # install plus two reboots took 1m40s in the spike
```

`Slot("incus-os", 2)` in `addressing.py`. After adding it, run
`sudo hitkit setup` once.

### 5.2 Certificates (host side, in `secrets/incus-os/`)

- `client.crt` / `client.key`: hitkit's own API client. Never leaves the host.
- `browser.crt` / `browser.key`, plus `browser.p12` and its password: for the
  web UI (section 7).
- `server.crt`: the server's self-signed certificate, pinned on first contact.

The client certificates are generated once with `get_or_create`. Only
certificates go into the seed.

### 5.3 Seed (new hook `seed(hctx, machine, plan) -> dict[str, dict]`)

The machine layer writes each file the hook returns as `<section>.json` into
`seed.img`, a 16 MiB FAT disk labelled `SEED_DATA` built with
`mkfs.fat -C -n SEED_DATA` and `mcopy`. The seed holds only public data;
IncusOS stores seeds unencrypted. The exact files that worked are in
[incus-os-in-libvirt.md](../incus/incus-os-in-libvirt.md) section 2.
- **`install`:** `force_reboot: false`, `target: {id: hitkit-data}`.
- **`network`:**
  - one interface `uplink` matched by the slot's MAC, with address
    `<slot ip>/24`;
  - a default route via `.1`;
  - DNS: hostname `incus-os`, domain `hitkit.internal`, nameserver `.1`.

  Everything comes from `cfg.network()`, so `--subnet` keeps working.
- **`incus`:** `apply_defaults: true` (local pool, `incusbr0`, listen on
  `:8443`), with the `hitkit` and `hitkit-browser` client certificates trusted.
- **`security`:** `custom_ca_certs: [hitkit CA]`, so instances pulling from the
  hitkit cache or forge verify TLS.
- **No `kernel`, `update` or `applications` files.** IncusOS uses its own
  defaults. A `kernel` console entry is not needed and, written as `ttyS0`,
  breaks startup.

### 5.4 Incus API client

A small `src/hitkit/incus.py`, modelled on `forgejo.py`: urllib with an
`ssl.SSLContext` holding hitkit's client certificate. It verifies the server by
the pinned certificate. It covers `/1.0`, certificates and trust tokens,
`/1.0/operations` (pending tokens), and the IncusOS endpoints under
`/os/1.0/…` (security, storage). No secrets go into argv; the trace logs
requests the way the forge client does.

## 6. Hooks

### 6.1 New hook methods (defaults in `kits.Hooks`)

| Method | Purpose |
|---|---|
| `seed(hctx, machine, plan)` | seed files for installer roles |
| `probe_ready(hctx, machine) -> str \| None` | `None` when ready, otherwise a reason; polled by `_wait_ready` for installer roles |
| `next_steps(hctx, plan) -> list[str]` | replaces the generic `Next: hitkit creds … hitkit ssh …` lines after `up` |

### 6.2 Readiness and server pinning

`probe_ready` calls `GET https://<slot ip>:8443/1.0` with hitkit's client
certificate and is ready when `metadata.auth == "trusted"`. On the very first
contact, with no pin yet, it records the server's certificate as `server.crt`.
That contact runs over the host-only bridge within seconds of the install. This
is an accepted trust-on-first-use risk, added to the design's threat model.
Every later connection checks the pin. A destroy that deletes the data also
deletes the pin.

### 6.3 after_ready (idempotent)

- Fetch `/os/1.0/system/security`. Store the encryption recovery key and the
  `local` pool's recovery key as secrets the first time. Reading them also
  silences IncusOS's warning that the keys were never retrieved.
- Ensure the CLI trust token (section 6.4).

### 6.4 Remote setup, tokens and `up` output

- **Tokens.** hitkit mints a token with `POST /1.0/certificates` and builds the
  token string itself:
  `base64(JSON {client_name, fingerprint, addresses: ["incus-os.hitkit.internal:8443"], secret, expires_at})`.
  The single DNS address gives the remote a clean URL. Without it the server's
  address list would include `incusbr0` and IPv6 addresses the host cannot
  reach.
- **The CLI token.** hitkit keeps at most one pending token named
  `hitkit-incus-os-cli`, as a secret. `up` reprints it while it is still pending
  (the running `token`-class entries in `/1.0/operations`), and stops once it
  has been used.
- **More tokens.** `token_create`, `token_list` and `token_revoke` mint, list
  and revoke trust tokens, so `hitkit token create incus-os --name laptop` works
  the way it does for forge.
- **`next_steps` after a first `up`:**
  ```
  ✓ incus-os is ready (1m52s)

  Add it to your incus CLI:
    incus remote add hitkit-incus-os <token>
    incus launch images:debian/trixie hitkit-incus-os:test
  Web UI: https://incus-os.hitkit.internal:8443  (import the browser certificate: hitkit creds incus-os)
  More tokens: hitkit token create incus-os --name <name>
  ```
  Once the token has been used, the first block becomes
  `incus remote switch hitkit-incus-os` (or `incus remote list` to check).
- **Not used:** `incus remote add <name> <url> --token <token>`. It still asks
  for the fingerprint interactively.
- **Reach.** The remote works from the workstation running hitkit. The bridge
  is not reachable from the LAN.

### 6.5 creds

`hitkit creds incus-os` (and `--json`) shows:
- `url`, `ui`, `remote_add` (the command above, while a token is pending),
  `server_fingerprint`;
- `ui_certificate` (path to `browser.p12`), `ui_certificate_password`,
  `ui_import` (a one-line browser hint);
- `encryption_recovery_key`, `pool_recovery_key`;
- `shell`: "none, use incus admin os".

### 6.6 backup_plan

Offline, the default: disk plus NVRAM and TPM sidecar.

## 7. Web UI

- **Shipped already.** IncusOS bundles the Incus UI. With `apply_defaults`
  Incus listens on `:8443`. The UI is at
  `https://incus-os.hitkit.internal:8443/ui/`.
- **Login.** The UI authenticates with a TLS client certificate. hitkit makes a
  separate `hitkit-browser` certificate, trusted through the seed, rather than
  exporting its own API key into a browser store. That way each can be revoked
  without breaking the other. `hitkit browser-cert incus-os` imports it into every Firefox
  profile and Chromium's `~/.pki/nssdb` with the NSS tools, no root needed (a client identity
  cannot live in the system trust store). It writes `browser.p12` with a generated password,
  using `cryptography`'s `BestAvailableEncryption`; NSS accepts that bundle. You
  import it once (Firefox: Settings → Privacy & Security → Certificates → Your
  Certificates → Import; Chromium: Settings → Privacy and security → Manage
  certificates).
- **Server trust.** The server's certificate is self-signed (we stay
  standalone), so the browser asks for a one-time exception. `creds` prints the
  fingerprint to compare.
- **Not chosen:**
  - The UI's in-browser certificate generation plus a trust token: still
    manual, and the key lives only in the browser.
  - OIDC: needs an identity provider.
  - A single-node cluster for a hitkit-CA certificate (section 1).

## 8. Host

- **`doctor`.** Adds `swtpm` to the package lists (Debian and Fedora also need
  `swtpm-tools`). It checks that a secure-boot OVMF firmware descriptor exists
  (on Arch, `50-edk2-ovmf-x86_64-secure-4m.json`). Both are reported as needed
  only for incus-os.
- **Nested virtualisation.** Incus VMs inside IncusOS need nested KVM, which
  `doctor` already checks for proxmox; the spike launched one. Containers work
  without it.
- **Design docs.** §7.2 and `docs/kits.md` §4 list slot `.2` for incus-os.

## 9. Docs and tests

- **Docs:**
  - `docs/kits.md`: a section on installer roles (`installer.toml`, seed
    hook, probe, state sidecar, the single disk being `data.qcow2`), and the
    slot list.
  - `docs/design.md`:
    - §7.2, the slot;
    - a new §13 entry for IncusOS;
    - the threat model's accepted risks: trust on first use for the incus-os
      server certificate;
    - §8.1's version table.
  - `README.md`: an "IncusOS" usage section.
  - `skills/hitkit/references/kits.md`.
  - `docs/incus/`: keep up to date when versions move or new behaviour is found.
- **Unit tests:**
  - `test_kits`: the kit parses, and the slot exists.
  - `test_image_pins`: installer pins parse, and `role_dirs` / hash cover
    `installer.toml`.
  - `test_domain`: installer XML (SCSI, TPM with a dir source, secure boot
    with no enrolled keys, SMM, USB media and boot order, no graphics, no
    fw_cfg).
  - A new `test_seed`: the FAT image has label `SEED_DATA` and the
    `<section>.json` files, read back with `mdir`/`mtype`.
  - `test_durability`: the NVRAM and TPM sidecar is kept, reused and backed up.
  - A new `test_incus_os`:
    - seed content from the slot and subnet;
    - certificates idempotent;
    - `creds` before `up` raises;
    - token string format and address;
    - token hooks against a fake API;
    - `probe_ready` and pinning against a fake server;
    - `next_steps` before and after the token is used.
- **e2e** (`HITKIT_E2E=1`):
  - `up incus-os`;
  - the remote add with the printed command;
  - launch a container;
  - `destroy` and `up` again from the kept disk without a recovery key;
  - backup and restore;
  - grow the disk and reboot.

## 10. Phases

0. **Spike.** Done (section 3).
1. **Installer machines** in the machine layer (section 4), with unit tests and
   a throwaway kit if useful.
2. **The incus-os kit**: kit.toml, slot, seed, API client, hooks, creds, tokens,
   web UI certificate, `up` output.
3. **Durability**: sidecar keep, reuse, backup and restore; recovery keys.
4. **Doctor, docs, e2e**, including the items the spike did not cover
   (section 11).

## 11. Risks and open items

- **The install handshake** matches console text and depends on IncusOS
  behaviour that may change between releases. Pin bumps get an e2e run. If the
  text changes, the `IncusOSInstallComplete` UEFI variable in `nvram.fd` is a
  fallback signal.
- **Losing NVRAM or TPM state** means typing a recovery key. Section 4.5 and the
  stored keys cover this; the e2e test proves it.
- **Session-end save with a TPM is untested.** libvirt saves running session
  VMs at logout and resumes them later. Check that this works and that IncusOS
  comes back unlocked before relying on it.
- **Other hosts are untested.** Debian 13 and Fedora 44: `<source type='dir'>`
  on the TPM backend, the secure-boot firmware descriptor names, and swtpm under
  session libvirt. Run them in the nested e2e jobs.
- **Updates run outside hitkit.** IncusOS updates itself, so `hitkit status`
  shows the installed version, not the running one, unless a later change asks
  the API. A self-update and its reboot are untested.
- **The IncusOS API is marked "subject to change".** Keep the hitkit calls few
  (`/1.0`, certificates, operations, `/os/1.0/system/security`,
  `/os/1.0/system/storage`).
