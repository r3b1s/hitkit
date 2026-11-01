# Plan: the incus-cluster kit

Status: **implemented**. The evidence for every step is in
[docs/incus/operations-center.md](../incus/operations-center.md).

Goal: `hitkit up incus-cluster` gives an IncusOS cluster managed by
[Operations Center](https://github.com/FuturFusion/operations-center) (OC), with OC running as
an Incus VM on the cluster instead of as a second libvirt VM.

## 1. Decisions

- **Names.** Kit `incus-cluster`, machines `incus-cluster-1` to `-3` (slots `.3`–`.5`), VM and
  slot `operations-center` (`.6`), Incus remote `hitkit-incus-cluster`.
- **`--nodes`, default 1, maximum 3.** One node keeps `up` cheap; more exercise the real join path.
- **OC forms and manages the cluster.** OC cannot adopt a cluster, so a hitkit-formed cluster
  would leave OC idle. This fixes the install order in section 2.
- **Reuse the `incus-os` role.** The nodes install the same pinned image as the `incus-os` kit.
- **Seed inside a pinned ISO.** `kits/incus-cluster/operations-center.toml` pins the ISO by version
  and sha256, kept out of `installer.toml` so the role hash stays put. A test asserts the two
  versions match. hitkit writes the seed tar into the image's empty seed slot. The customizer
  service is not used: it is a network dependency and an unpinned image.
- **Only public data in seeds.** OC's registration token and the hitkit-CA keys go over the API.

## 2. Install order

1. Install the nodes: no Incus defaults, `core.https_address` in the preseed.
2. On the first node: pool `bootstrap`, a volume for logs on it, and
   `storage.logs_volume=bootstrap/logs`.
3. Upload the seeded ISO and install OC in a VM on `bootstrap`.
4. Give OC its hitkit-CA certificate, then wait for its first update.
5. Register the first node, and have OC form a cluster of that one node.
6. Stop the VM, set `storage.logs_volume=local/logs`, move the VM to `local`, start it, delete
   `bootstrap`.
7. Register the other nodes and join them. This must follow step 6: a joining member needs
   every pool.
8. Give the cluster its hitkit-CA certificate, and mint the CLI token.

## 3. What the spike changed

The first version of this plan was written from upstream's docs and source. Testing changed it:

| Planned | Built | Why |
|---|---|---|
| No handling of `storage.logs_volume` | Pin it to `bootstrap/logs` before the VM starts, move it during the stop window | Incus refuses to change it while an instance runs, and OC cannot stop its own VM |
| Certificates last | OC's first, the cluster's last | Nodes then verify OC through the hitkit CA, so no pin is sent |
| `server_certificate` in the provider config | Left out | Not needed once OC has a hitkit-CA certificate |
| `PUT /1.0/system/certificate` | `POST` | OC's API |
| Installer patterns matched on the raw console | Escape codes stripped first | On a wide console the TUI repaints word by word |
| The next step straight after a join | Wait for OC to settle, and retry the certificate write | OC's database stalls for minutes after a join; `hitkit up --nodes 2` found this, the hand-run spike did not |

## 4. The code

- `src/hitkit/kits/incus-cluster/hooks.py`: the kit. `finish` runs section 2's steps from 2 on,
  each reading live state, so an interrupted `up` resumes.
- `src/hitkit/operations_center.py`: the OC client, over `IncusClient`.
- `src/hitkit/incusos.py`: what the two IncusOS kits share, moved out of `kits/incus-os/hooks.py`.
- `src/hitkit/incus.py`: `put`, `patch`, `wait`, ISO upload, instance calls, `PinMismatch`.
- `src/hitkit/machine/seed.py`: `seed_tar`, `seeded_iso`.

## 5. Limits

- **Members cannot be removed.** `up` with a smaller `--nodes` refuses before changing anything.
- **`hitkit destroy incus-cluster --delete-data` is the only reset.** A cluster whose OC setup
  failed cannot be retried, and one member cannot be reinstalled while another's disk is kept.
- **OC is not a libvirt machine.** `hitkit status` does not list it; `hitkit creds` does. It lives on
  the first node's disk, so `destroy`, `backup` and `restore` of that node include it.
- **Pin bumps need a live run.** The seed slot's offset is an upstream layout constant. `seeded_iso`
  refuses an image whose slot is not empty, but only `up` proves the rest.
- **The OC and cluster certificates last 397 days** (`LEAF_VALIDITY`), and nothing renews them.

## 6. Verification

- **Unit.** `tests/test_incus_cluster.py` runs `finish` against a model of Incus and OC that
  enforces what the spike found, cutting `up` off at every change, before and after it lands.
- **e2e.** `HITKIT_E2E=1 uv run pytest tests/e2e/test_kits.py -v -k incus_cluster`, with
  `HITKIT_E2E_DISRUPTIVE=1` for the reboot and the kept-disk tests.
