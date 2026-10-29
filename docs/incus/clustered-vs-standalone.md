# Clustered versus standalone Incus

Why this came up: the only way found to make an Incus server present a
certificate signed by the hitkit CA is to make it a cluster. This document
records how that works, what it changes, and the decision for the incus-os kit.
Versions are listed in [README.md](README.md).

## 1. Replacing the server certificate

- **Standalone.** `PUT /1.0/cluster/certificate` fails with `404` "open
  /var/lib/incus/cluster.crt: no such file or directory". Nothing else in Incus
  or IncusOS sets the server certificate: there is no seed field and no preseed
  field, and IncusOS has no shell to replace `server.crt`.
- **Single-node cluster.** Turning the server into a one-member cluster makes it
  work:
  1. `PATCH /1.0` with `{"config": {"cluster.https_address": "<ip>:8443"}}`.
  2. `PUT /1.0/cluster` with `{"server_name": "incus-os", "enabled": true}`,
     then wait for the operation.
  3. `PUT /1.0/cluster/certificate` with `{"cluster_certificate": <PEM>,
     "cluster_certificate_key": <PEM>}`, a leaf signed by the hitkit CA for the
     DNS name and the IP.
- **Result.** The server then presents `CN=incus-os.hitkit.internal`, issued by
  the hitkit CA, verifies against hitkit's `ca.crt`, and keeps the certificate
  across reboots. New trust tokens carry the new fingerprint.
- **Remotes added before the swap break** ("x509: certificate signed by unknown
  authority"), so the swap must happen before any token is handed out.
- **As far as we know, clustering cannot be undone** on an Incus server. Not
  tested.

## 2. Comparison

The same 24 operations were run against a single-node cluster and a freshly
installed standalone server: both IncusOS `202610272235`, same seed apart from
address and hostname.

**Same on both:**
- creating and deleting storage pools (`zfs` with `source=local/<name>`, and
  `dir`), each in one command;
- creating and deleting a bridge network, in one command;
- projects, profiles and custom storage volumes;
- launching a container (`images:alpine/3.22`), `exec`, snapshots, copying an
  instance;
- launching a VM (nested KVM works);
- setting pool options without `--target`.

**Different:**

| | Single-node cluster | Standalone |
|---|---|---|
| `incus list` | extra `LOCATION` column | no such column; `-c L` fails: "Can't specify column L when not clustered" |
| `incus info <instance>` | includes `Location: incus-os` | no location line |
| API `location` on an instance | `"incus-os"` | `"none"` |
| `locations` on pools and networks | `[incus-os]` | `[none]` |
| `incus cluster list` | one member, `database-leader,database`, ONLINE | "Server isn't part of a cluster" |
| `incus storage show local` | `config: {}`; member-specific keys (`source`, `zfs.pool_name`, `volatile.initial_source`) appear only with `--target incus-os` | those keys appear directly |
| `used_by` URLs | member-local objects (images, custom volumes) end in `?target=incus-os` | plain URLs |
| `/1.0` `environment.server_clustered` | `true` | `false` |

**A correction to what we assumed.** We expected a single-node cluster to need
the two-step creation of storage pools and networks (first per member with
`--target`, then a second command without it). It does not: one command works,
as on a standalone server. The two-step flow belongs to clusters with more than
one member. That was not tested here.

## 3. Decision for the incus-os kit: standalone

Day-to-day use is the same either way. The differences show up in automation
that reads state: a script, Terraform or Ansible check that reads a pool's
`source` from `storage show`, uses the `LOCATION` column, or compares
`location` values behaves differently on a cluster than on a typical single
server. The kit is for testing against a normal Incus server, so it stays
standalone.

What that costs:
- **Trust on first use.** hitkit pins the server's self-signed certificate on
  its first connection, over the host-only bridge seconds after the install.
  This is an accepted risk.
- **A browser warning.** The browser shows a warning for the self-signed
  certificate once; hitkit prints the fingerprint to compare.
- **The `incus` CLI is unaffected.** It is verified through the fingerprint
  carried in the trust token.

## 4. For a future clustered kit

- The certificate swap in section 1 gives every member a hitkit-CA certificate.
  It is cluster-wide, so do it once, before handing out tokens.
- Test the two-step creation of pools and networks on two or more members, and
  how `--target` and `?target=` appear in reads.
- Joining members uses cluster join tokens (`POST /1.0/cluster/members`), not the
  client trust tokens in
  [incus-os-in-libvirt.md](incus-os-in-libvirt.md) section 8. Not tested.
- IncusOS documents clustering in its tutorial `incus-cluster` (in the IncusOS
  docs, under tutorials).
