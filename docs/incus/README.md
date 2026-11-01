# Incus and IncusOS notes

What we have learned about running [Incus](https://linuxcontainers.org/incus/)
and [IncusOS](https://linuxcontainers.org/incus-os/) under hitkit. These notes
are not tied to a single kit. They record facts that were tested, with the
versions they were tested on, so later kits (a clustered Incus kit, for example)
start from evidence rather than memory.

| Document | Covers |
|---|---|
| [incus-os-in-libvirt.md](incus-os-in-libvirt.md) | Installing and running IncusOS as a libvirt session VM: image, seed, domain XML, install flow, TPM and NVRAM, disk growth, API, trust tokens, web UI |
| [clustered-vs-standalone.md](clustered-vs-standalone.md) | What changes when an Incus server is a single-node cluster, and why the incus-os kit stays standalone |
| [operations-center.md](operations-center.md) | Running Operations Center as an Incus VM on the cluster it manages: seeding an image, the install order, what fails and why |

Related: the kit plans, [docs/plans/incus-os.md](../plans/incus-os.md) and
[docs/plans/incus-cluster.md](../plans/incus-cluster.md).

Everything here was tested on 2026-10-29 on an Arch/CachyOS host with libvirt
12.8, with IncusOS `202610272235` (Incus 7.5.1) and the Incus 7.5.1 client, and on
2026-10-31 with Operations Center 0.10.1 from the same IncusOS release.
Re-check facts against newer versions before relying on them. Each document
says which parts were not tested.
