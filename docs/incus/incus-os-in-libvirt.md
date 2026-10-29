# IncusOS in a libvirt session VM

How to install and run IncusOS as an unprivileged libvirt VM
(`qemu:///session`) on the `hitkit0` bridge, with full Secure Boot and TPM and
no degraded security mode. Everything below was done by hand, with no root
access beyond the existing `hitkit setup`. Versions are listed in
[README.md](README.md).

## 1. Image

- **Source.** `https://images.linuxcontainers.org/os/index.json` lists every
  release with `version`, `channels` (`stable`, `testing`, `daily`) and, for each
  file, its `filename`, `sha256` and `size`. The download URL is
  `https://images.linuxcontainers.org/os/<version>/<filename>`. For x86_64 the
  install image is the `image-raw` file, `x86_64/IncusOS_<version>.img.gz`; the
  sha256 is of the `.gz`. A signed copy of the index exists as `index.sjson`.
- **Pick the newest entry whose `channels` include `stable`.** The first entry
  in the list is not necessarily stable.
- **Size.** About 590 MiB gzipped, 3.2 GiB raw, 729 MiB once converted to
  qcow2. `gzip -dc | qemu-img convert -O qcow2` takes about 10 s.
- **Do not build it with mkosi.** A local build is signed with local keys and
  can never take official updates. See
  [docs/plans/incus-os.md](../plans/incus-os.md) section 2.

## 2. Seed

IncusOS has no interactive installer. It reads an install seed.

- **External seed disk.** A FAT (or ISO 9660) filesystem labelled `SEED_DATA`.
  IncusOS prefers it over the seed partition inside the install image, so the
  cached image never has to be modified. It can be built unprivileged:
  `mkfs.fat -C -n SEED_DATA seed.img 16384`, then `mcopy -i seed.img *.json ::/`.
  Long file names written by mtools work.
- **File names.** Each section is a file `<section>.json`, `<section>.yaml` or
  `<section>.yml`. JSON is decoded strictly: an unknown field is an error.
  Section names: `install`, `network`, `incus`, `security`, `kernel`,
  `applications`, `storage`, `services`, `update`, `provider`,
  `migration-manager`, `operations-center`.
- **Remove the seed disk after the install.** IncusOS cannot wipe an external
  seed, and the docs warn that one left attached confuses later boots.
- **The seed is stored unencrypted.** Put only public data in it.

Seed files that worked:

```json
// install.json
{"version": "1", "force_reboot": false, "target": {"id": "hitkit-data"}}
```

```json
// network.json
{"version": "1",
 "dns": {"hostname": "incus-os", "domain": "hitkit.internal",
         "nameservers": ["10.77.0.1"], "search_domains": ["hitkit.internal"]},
 "interfaces": [{"name": "uplink", "hwaddr": "52:54:00:68:00:1e",
                 "addresses": ["10.77.0.30/24"],
                 "routes": [{"to": "0.0.0.0/0", "via": "10.77.0.1"}],
                 "roles": ["management", "instances"]}]}
```

```json
// incus.json
{"version": "1", "apply_defaults": true,
 "preseed": {"certificates": [
   {"name": "hitkit", "type": "client", "certificate": "-----BEGIN CERTIFICATE-----\n…"},
   {"name": "hitkit-browser", "type": "client", "certificate": "-----BEGIN CERTIFICATE-----\n…"}]}}
```

```json
// security.json
{"version": "1", "custom_ca_certs": ["-----BEGIN CERTIFICATE-----\n…"], "encryption_recovery_keys": []}
```

What each one did:
- **`install`.** `target.id` is a substring match against `/dev/disk/by-id/`. A
  SCSI disk with `<serial>hitkit-data</serial>` appears as
  `scsi-0QEMU_QEMU_HARDDISK_hitkit-data`. With `force_reboot: false`, IncusOS
  waits for the install media to disappear and then reboots by itself.
- **`network`.** Matched the interface by `hwaddr` and applied the static
  address, route and DNS. The Incus server name became
  `incus-os.hitkit.internal`.
- **`incus`.** `apply_defaults: true` creates the `local` ZFS pool on the
  system disk's free space and the `incusbr0` NAT bridge, and listens on
  `:8443` on every interface. At least one client certificate is required, or
  nothing can authenticate.
- **`security`.** `custom_ca_certs` added the hitkit CA to IncusOS's trust store.
- **Do not send `kernel` with `"device": "ttyS0"`.** incus-osd runs
  `stty -F ttyS0` with the value exactly as given, fails, and refuses to start
  ("critical startup error"). A full path (`/dev/ttyS0`) is presumably what it
  expects; not tested. No kernel seed is needed anyway (see section 3).
- **Updates.** With no `update` seed, the updater uses the `stable` channel, a
  `6h` check frequency and `auto_reboot: false`.

## 3. Domain

The pieces that differ from a normal hitkit domain:

```xml
<os firmware='efi'>
  <type arch='x86_64' machine='q35'>hvm</type>
  <firmware>
    <feature enabled='yes' name='secure-boot'/>
    <feature enabled='no' name='enrolled-keys'/>
  </firmware>
  <nvram>…/nvram.fd</nvram>
</os>
<features><acpi/><apic/><smm state='on'/></features>
<devices>
  <controller type='scsi' model='virtio-scsi'/>
  <controller type='usb' model='qemu-xhci'/>
  <disk type='file' device='disk'>            <!-- system disk, blank qcow2 -->
    <driver name='qemu' type='qcow2' discard='unmap'/>
    <source file='…/data.qcow2'/>
    <target dev='sda' bus='scsi'/>
    <serial>hitkit-data</serial>
    <boot order='1'/>
  </disk>
  <disk type='file' device='disk'>            <!-- install media: overlay on the cached image -->
    <driver name='qemu' type='qcow2'/>
    <source file='…/install.qcow2'/>
    <target dev='sdb' bus='usb' removable='on'/>
    <boot order='2'/>
  </disk>
  <disk type='file' device='disk'>            <!-- seed -->
    <driver name='qemu' type='raw'/>
    <source file='…/seed.img'/>
    <target dev='sdc' bus='usb' removable='on'/>
  </disk>
  <tpm model='tpm-crb'>
    <backend type='emulator' version='2.0' persistent_state='yes'>
      <source type='dir' path='…/tpm'/>
    </backend>
  </tpm>
  <!-- serial console logged to a file; no graphics device -->
</devices>
```

- **Firmware.** Autoselection with `secure-boot=yes` and `enrolled-keys=no`
  picked `OVMF_CODE.secboot.4m.fd` with the blank `OVMF_VARS.4m.fd` template.
  Microsoft's keys are not enrolled, so the firmware starts in setup mode.
- **Disks must be SCSI (virtio-scsi) or SATA.** IncusOS does not see virtio-blk
  disks the way it sees physical ones.
- **TPM.** swtpm runs as the user under session libvirt. `<source type='dir'>`
  keeps its state (`tpm2-00.permall`) in a directory of our choosing.
- **No graphics device.** Then `/dev/console` is the serial port, and the
  IncusOS status screen (TUI) and the journal on failure appear in the serial
  log with no kernel seed.
- **Boot order.** The empty system disk comes first; the firmware falls
  through to the USB media while the disk has nothing to boot. After the
  install the system disk boots.

## 4. Install flow and timing

1. **First boot (from USB).** systemd-boot enrolls IncusOS's Secure Boot keys
   from `\loader\keys\auto` ("Custom Secure Boot keys successfully enrolled,
   rebooting the system now!") and reboots.
2. **Second boot.** The install runs: it wipes the target, clones the GPT,
   copies the ESP and five partitions. It took about 35 s. It then prints
   "IncusOS was successfully installed." and "Please remove the install media
   to complete the installation." to the console, and also sets the UEFI
   variable `IncusOSInstallComplete` (visible as UTF-16LE in `nvram.fd`).
3. **Remove the media.** `virsh detach-disk <dom> sdb --live --config` and the
   same for `sdc`. IncusOS notices the device going away and reboots into the
   installed system.
4. **First boot of the installed system.** `GET /1.0` answered 57 s after the
   detach.

From `virsh start` to a trusted API: about 1 min 40 s. A later normal boot to a
trusted API: 22 s. The console text in step 2 survives the TUI's escape codes
well enough to match on. If the media is detached late, IncusOS's "already
installed, please remove the media" path waits for the removal, then reboots.

## 5. State: NVRAM, TPM and the disk belong together

- **Encryption.** Root and swap are encrypted and unlocked by the TPM
  ("unlocked (TPM)"), with the key sealed to measurements that include the
  Secure Boot state in NVRAM.
- **Keeping all three works.** `virsh undefine --keep-nvram --keep-tpm`, then
  defining the domain again without the install disks, booted unlocked by the
  TPM.
- **Losing the TPM state stops the boot.** With an empty TPM directory it
  stopped at "Please enter passphrase or recovery key for disk root-x86-64
  (root)".
- **Store the recovery keys.** `GET /os/1.0/system/security` returns
  `config.encryption_recovery_keys` and `state.pool_recovery_keys` (`local`).
  Until they are retrieved, the console warns "Some encryption recovery keys
  have not been retrieved yet!".
- **Security state reported:** `secure_boot_enabled: true`, `tpm_status: ok`,
  `system_state_is_trusted: true` ("system state is fully trusted"). IncusOS
  treats swtpm presented by QEMU as a real TPM, so no degraded mode is needed.

## 6. Disk and storage

- **Partitions.** The install creates five partitions plus the `local` ZFS
  pool partition (part 11) on the rest of the disk. The system partitions use
  about 34 GiB: a 150 GiB disk gave a 116 GiB pool.
- **Growing.** A live `virsh blockresize` from 150 to 200 GiB grew the drive
  (`/os/1.0/system/storage` reported a 200 GiB capacity) but not the pool.
  After one reboot the `local` pool was 166 GiB.
- **Reading storage state.** `GET /os/1.0/system/storage` returns
  `config.pools[]` (with `raw_pool_size_in_bytes` and
  `usable_pool_size_in_bytes`) and `state.drives[]`.

## 7. API

- **IncusOS's API is served through Incus's** at `https://<host>:8443/os/1.0/…`,
  with the same client certificate. The `incus admin os …` commands use it. It
  is marked "subject to change".
- **Readiness.** `GET /1.0` with a trusted client certificate answers
  `metadata.auth: "trusted"`; without one, `"untrusted"`.
- **The server certificate is self-signed** (`O=Linux Containers,
  CN=root@<hostname>`). On a standalone server it cannot be replaced; see
  [clustered-vs-standalone.md](clustered-vs-standalone.md).

## 8. Trust tokens and `incus remote add`

- **Minting.** `POST /1.0/certificates` with
  `{"name": "<name>", "type": "client", "token": true}` returns an operation.
  Its `metadata.metadata` holds `addresses`, `fingerprint`, `secret`,
  `expires_at` and `request` (whose `name` is the client name).
- **Token format.** The token string the CLI accepts is
  `base64(JSON {"client_name": <request.name>, "fingerprint", "addresses",
  "secret", "expires_at"})`. Leaving `client_name` empty or null makes the
  client treat the token as a host name.
- **No prompt.** `incus remote add <name> <token>` adds the remote, checks the
  fingerprint in the token, and asks nothing.
- **A wrong fingerprint is refused:** "Certificate fingerprint mismatch between
  certificate token and server".
- **The token's addresses decide the remote URL.** The client connects to them
  and the remote's URL becomes the first address. The server lists every
  address it listens on, including `incusbr0` and IPv6 addresses the host
  cannot reach. Rebuilding the token with only the address you want (tested
  with `["10.77.0.30:8443"]`) works and gives a clean remote URL.
- **`incus remote add <name> <url> --token <token>` still asks for the
  fingerprint interactively.** It is not suitable for a scripted, verified setup.
- **Pending tokens** are the running operations of class `token` in
  `GET /1.0/operations?recursion=1`.
- **Changing the server certificate later breaks existing remotes**, because the
  client pins the old one ("x509: certificate signed by unknown authority").

## 9. Web UI

- **Bundled.** The Incus UI ships with IncusOS's Incus application at
  `https://<host>:8443/ui/` (page title "Incus UI").
- **Login.** It authenticates with a TLS client certificate trusted by the
  server. A dedicated browser certificate in the `incus` seed works.
- **Export.** A PKCS#12 bundle from Python's `cryptography`
  (`pkcs12.serialize_key_and_certificates(…, BestAvailableEncryption(pw))`:
  PBES2, AES-256-CBC, PBKDF2-HMAC-SHA256, SHA-256 MAC) imports into NSS with
  `pk12util`. NSS is the store Firefox and Chromium use on Linux.
- **No system-wide import.** A login certificate is an identity with a private key,
  not a trust anchor, so p11-kit's trust store cannot hold it. Firefox has an NSS
  database per profile and Chromium shares `~/.pki/nssdb`; `pk12util -i … -d sql:<db>`
  imports into each without a browser dialog (`hitkit browser-cert` does this).
  Re-importing the same bundle is harmless; a database with a primary password fails
  with `SEC_ERROR_BAD_PASSWORD` rather than prompting when given `-k /dev/null`.
- **Not tested:** logging in through an actual browser window.

## 10. Not tested

- **Session-end save.** libvirt saves running session VMs when the session ends
  and resumes them later (`auto_shutdown_try_save`). Not tried with a TPM.
- **Other hosts.** Debian 13 and Fedora 44, whose libvirt and firmware file
  names differ: `<source type='dir'>` support and the secure-boot firmware
  descriptor need checking there.
- **Updates.** An IncusOS self-update and its reboot.
- **Kernel seed.** A `kernel` seed with `/dev/ttyS0`.
