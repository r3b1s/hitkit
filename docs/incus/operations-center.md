# Operations Center on an IncusOS cluster

How to run [Operations Center](https://github.com/FuturFusion/operations-center) (OC) as an
Incus VM on the cluster it manages, with no second libvirt VM. Everything here was done by
hand against throwaway VMs (`virsh` on `qemu:///session`, `hitkit0`, addresses `.30`–`.33`).
Versions are in [README.md](README.md): IncusOS `202610272235`, OC 0.10.1. Section 8 lists what
was not tested.

## 1. What Operations Center is

- **An IncusOS primary application.** Incus, OC and Migration Manager exclude each other on
  one IncusOS, so OC needs its own IncusOS. "OC as an Incus VM" means a nested IncusOS: host →
  libvirt IncusOS node → Incus VM → IncusOS with OC.
- **It cannot adopt a cluster.** It manages only clusters it formed itself from servers
  registered to it (`POST /1.0/provisioning/clusters`). The nodes must not be clustered by hand.
- **Running on the cluster it manages is a supported shape.** Stéphane Graber: "We pretty
  commonly run Operations Center on top of the cluster it manages." IncusOS and OC avoid hard
  boot-time dependencies on each other, and a rolling reboot keeps its state in the database.

## 2. Seeding an image

- **Seed slot.** An IncusOS image has an empty slot at byte `2148532224`, the same in the
  `.img` and `.iso` forms. A plain tar of `<section>.json` files written there is the seed.
  OC's own flasher uses the same constant. In `202610272235` the slot is all zeros.
- **No customizer needed.** The tar can be written with any language, so hitkit can seed the
  image it pinned by sha256 instead of downloading an unpinned one from the customizer service.
- **Seed that worked** (the network section is as in
  [incus-os-in-libvirt.md](incus-os-in-libvirt.md) §2, with roles `["management"]`):

```json
// applications.json
{"version": "1", "applications": [{"name": "operations-center"}]}
```

```json
// operations-center.json
{"version": "1", "apply_defaults": true, "preseed": null,
 "trusted_client_certificates": ["-----BEGIN CERTIFICATE-----\n…"]}
```

- **OC's defaults are right.** With no `system_network` it listens on `[::]:8443` and uses its
  management address as `address`.

## 3. Installing in an Incus VM over REST

- **ISO import.** `POST /1.0/storage-pools/<pool>/volumes/custom`, the raw file as the body, with
  `Content-Type: application/octet-stream`, `X-Incus-name` and `X-Incus-type: iso`. It returns an
  operation. 3.2 GiB took 11 s.
- **VM.** `security.secureboot=false` only leaves out Microsoft's keys, so IncusOS can enroll
  its own; Secure Boot is on afterwards. Add a `tpm` device and the ISO with `boot.priority=10`.
- **Result.** `secure_boot_enabled: true`, `tpm_status: ok`, `system_state_is_trusted: true`.
- **Network.** `nictype=bridged parent=uplink hwaddr=<mac>` with a static address in the seed is
  reachable from the host on `hitkit0`. The node's `uplink` needs the `instances` role.
- **Detecting the end of the install.** `GET /1.0/instances/<n>/console?type=log` gives the
  console. At 160 columns the TUI repaints word by word, with cursor moves between the words, so
  `was successfully installed.` never appears as one string in the raw log. It matches once
  the escape codes are stripped. `?type=vga` returns a PNG, useful for debugging.
- **Removing the media.** Stop the VM (`force`, it is waiting for the removal), `PUT` the instance
  without the device, start it.
- **OC says `auth: "tls"`** on `GET /1.0`, not Incus's `"trusted"`.

## 4. The order that works

Each step exists because the obvious alternative failed; section 5 has the failures.

1. Install the nodes with `apply_defaults: false`, and `core.https_address: ":8443"` in the
   preseed (without defaults nothing listens). They come up with no pools and no networks, and
   `uplink` appears as an unmanaged bridge.
2. On the first node create pool `bootstrap` (`source=local/bootstrap`) and a volume `logs` on it,
   and set `storage.logs_volume=bootstrap/logs`.
3. Create the OC VM on `bootstrap` and install it. Wait for an update in state `ready`.
4. Give OC a certificate from a CA the nodes trust (`POST /1.0/system/certificate`).
5. Mint a token. Register the first node with `PUT /os/1.0/system/provider`:
   `{"config": {"name": "operations-center", "config": {"server_url": …, "server_token": …}}}`.
   It reaches `ready` in OC within seconds.
6. `POST /1.0/provisioning/clusters` with only that node, and an application seed of
   `{"config": {"storage.logs_volume": "bootstrap/logs"}}`. The VM keeps running throughout. 13 s.
7. Stop the VM. Set `storage.logs_volume=local/logs` on the member, move the VM with
   `POST /1.0/instances/<n>` (`{"name": …, "migration": true, "pool": "local"}`), start it, and
   delete the volumes and the `bootstrap` pool.
8. Register the other nodes, and `POST /1.0/provisioning/clusters/<n>/:add-servers`.
9. Give the cluster a certificate (`PUT /1.0/provisioning/clusters/<n>/certificate`).

The end state matches a cluster OC built normally: `local` on every member, the `backups`, `images`
and `logs` volumes, `meshbr0` and `incusbr0`, and the `terraform-configuration` artifact. OC's
inventory lists its own VM.

## 5. What failed, and why

- **OC's OpenTofu run creates, it does not import.** It always creates `local`, `incusbr0`, the
  `local/{backups,images,logs}` volumes and `meshbr0`, so a node with `apply_defaults: true` would
  fail. The first pool therefore cannot be called `local`.
- **`storage.logs_volume` cannot change while an instance runs.** OC's default sets it to
  `local/logs`, on a node running OC's own VM, and OC cannot stop the VM it runs in:
  "`storage.logs_volume` cannot be changed if there are running instances". The images and
  backups volumes have no such rule.
- **Sending `""` for that key does not work.** It passes validation, then the provider fails:
  "element "storage.logs_volume" has vanished". An unchanged non-empty value is a no-op, which
  is why step 2 sets it first and step 6 repeats it. Joining members do not use the seed: OC
  hard-codes `local/logs` for them, so the bootstrap value goes no further.
- **A certificate in the application seed that the node already trusts** fails with "Certificate
  already in trust store". Leave `certificates` out: the nodes trust hitkit from their own seed.
- **A failed run cannot be retried.** Clustering happens first, so OC keeps the cluster, shows it
  as `ready`, and refuses to create it again. The difference is that only a finished run stores the
  `terraform-configuration` artifact. Everything after the failing resource is missing too,
  because later resources depend on `incus_server.this_per_node`.
- **OC stalls after a join.** The join itself takes seconds, but OC then resyncs its inventory and
  holds its database for minutes (2.5 in the live run). `GET /1.0` still answers at once, while
  calls that touch the database time out or return a `500`, and then everything is instant again.
  So wait for a provisioning read to succeed before the next step, and retry writes. The hand-run
  spike missed this because its next call came minutes later; `hitkit up` found it.

## 6. Certificates

- **OC's is replaceable,** unlike a standalone Incus server's. `POST` (not `PUT`) takes a
  certificate and key.
- **OC keeps a separate client identity** for talking to servers, so replacing its server
  certificate does not lock it out of them.
- **Nodes do not need the `server_certificate` pin.** With OC already on a CA-signed certificate
  and that CA in `custom_ca_certs` from the seed, a node registered with no pin. A node holding a
  stale pin falls back to its trust store and drops the pin when the pin fails.
- **The cluster's certificate** can be replaced through OC, which keeps it in sync. Members
  joining afterwards present it too.
- **Right after the cluster swap,** OC lists the members as `offline` until its next check,
  seconds later. This is not a failure.

## 7. Timing and operations

A three-node test, nested on a host also running other VMs:

| Step | Time |
|---|---|
| Node install to trusted API | 100 s |
| Upload, nested install, media removal | 3–6 min |
| OC first boot | 4–7 min |
| First update `ready` | 5 min |
| Forming the cluster | 13 s |
| Stopping, moving (13 GiB, 25 s), OC back | 4–6 min |
| Joining a node | 6 s, then up to 2.5 min until OC settles |

Through `hitkit up` itself, one node took 18 minutes from nothing.

- **Reboot of the first node.** `boot.autostart=true` restarts the VM; OC answers about 4.5 minutes
  later, TPM trust intact.
- **Moving the VM between pools keeps its TPM state:** no recovery prompt.
- **`virsh managedsave` of a node with the nested VM running** works: 5 s each way, same state.
- **Instances** launch on any member with `--target`.
- **Noise.** OC logs "Failed to get token from IncusOS" and an inventory warning about `backups`
  on several members. Neither stopped anything.

## 8. Not tested

- Evacuation. TPM devices cannot live-migrate, so upstream says the OC VM stops and restarts.
- An OC-driven rolling update of the cluster that hosts OC.
- `hitkit` hosts other than Arch/CachyOS.
