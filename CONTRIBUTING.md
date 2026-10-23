# Contributing to hitkit

hitkit has one maintainer and is opinionated by design: the curated kits and the
threat model in [docs/design.md](docs/design.md) are the product. Contributions are
welcome within that.

## Bugs and questions

Open an issue at <https://github.com/r3b1s/hitkit/issues>. A good bug report has:

- what you ran and what happened, with the trace id hitkit printed
- the output of `hitkit trace --failed last` (secrets are masked, but check before
  pasting)
- your host distribution and `hitkit doctor` output

Report security vulnerabilities privately instead; see [SECURITY.md](SECURITY.md).

## Changes

Open an issue to discuss a change before sending a pull request, so that work is not
spent on something that does not fit. Small fixes (typos, obvious bugs) can go straight
to a pull request.

All changes reach `main` through pull requests.

- Keep a pull request to one change, with tests for new behaviour.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, …), as the existing history does.
- Pinned versions (`pins.toml`, apt versions in `mkosi.conf`, container images) follow
  [AGENTS.md](AGENTS.md): the latest stable release, pinned by exact version and sha256
  or image digest.

By contributing, you agree that your contributions are licensed under the project's
[Apache License 2.0](LICENSE).

## Development setup and tests

```sh
git clone https://github.com/r3b1s/hitkit.git && cd hitkit
uv sync                                    # needs your distribution's libvirt development package
uv run pytest                              # unit tests: no VMs, no root, a few seconds
HITKIT_E2E=1 uv run pytest tests/e2e -v    # end-to-end: against live kits on this host
```

The unit tests need no virtualisation and are what every change must pass. The
end-to-end tests are skipped unless `HITKIT_E2E=1` is set. They run against kits already
up on the host: `hitkit up forge --ephemeral 1` at least, with the hitkit CA trusted
(`sudo .venv/bin/hitkit trust install`); the cache and Proxmox tests skip unless those
kits are up. Run them before changing images, kits or the machine layer, either here or
through local CI (below), which needs no kits on your own host.

## Continuous integration

Every pull request and every push to `main` runs these checks on GitHub-hosted runners;
the branch ruleset on `main` requires them to pass before merging:

| Check | Workflow | What it does | Run it locally |
|---|---|---|---|
| Unit tests | `ci.yml` | `uv sync --locked` and `uv run pytest` on Python 3.12 | `uv run pytest` |
| Workflow lint (zizmor) | `ci.yml` | security problems in `.github/workflows` | `zizmor .github/workflows` |
| SAST (CodeQL, Python) | `codeql.yml` | security weaknesses in the Python code | results are in the pull request and the Security tab |
| Dependency scan (OSV-Scanner) | `osv-scanner.yml` | known-vulnerable or malicious packages in `uv.lock` | `osv-scanner --lockfile=uv.lock` |

A failing check shows its output in the pull request's Checks tab; CodeQL and
OSV-Scanner findings are also listed under the repository's Security tab. How findings
are handled is in [SECURITY.md](SECURITY.md#scanning-and-remediation-policy).

The end-to-end tests do not run on GitHub, by choice: they need KVM, root for host setup
and image builds. They run in local CI instead.

A weekly workflow, `osps-baseline.yml`, also assesses the repository against the OSPS
Baseline; it never runs on pull requests.

## Local CI

The maintainer checks changes on a hitkit forge on their own desktop before pushing them
to GitHub. `.forgejo/workflows/ci.yml` runs the unit tests, then five end-to-end jobs,
each in a fresh Arch job VM: `.forgejo/scripts/nested-e2e.sh` runs `hitkit setup` and
brings up kits *nested* in that VM (on `10.78.0.0/24`), then runs their tests from
`tests/e2e`. One job tests the cache and forge kits, one NetBird, one Ludus, one
Proxmox Backup Server, and one Proxmox Backup Server added to Proxmox VE. GitHub ignores `.forgejo/`. Forgejo runs only `.forgejo/workflows`
on a branch that has it; on a branch without it, Forgejo would try `.github/workflows`,
and those jobs fail locally.

One-time setup, on a host with nested virtualization:

1. Give Arch job VMs hitkit's host toolchain, in `~/.config/hitkit/images/runner-arch.toml`:

   ```toml
   packages = [
       "apt", "debian-archive-keyring", "dnsmasq", "dosfstools", "edk2-ovmf", "libvirt",
       "mkosi", "mtools", "nftables", "pkgconf", "qemu-base", "systemd-ukify", "uv",
   ]
   ```

2. Bring up the forge with Arch job VMs large enough for nested kits. Job VMs are
   sized like the runners; two run at a time:

   ```sh
   hitkit up forge --ephemeral 2 --arch --memory runner=16384 --cpus runner=8 --disk runner=100
   ```

3. Create a private repository `<you>/hitkit` on the forge, and add it as a remote:

   ```sh
   git remote add forgejo forgejo.hitkit.internal:<you>/hitkit.git
   ```

For each change:

```sh
git push forgejo <branch>     # results: https://forgejo.hitkit.internal/<you>/hitkit/actions
git push origin <branch>      # only once local CI passes; then open the pull request on GitHub
```

A push runs every job. When only some kits need testing again (say, after fixing one
kit's test), push with `[skip ci]` in the commit message and start the workflow by hand:
Actions > local-ci > Run workflow, with only those kits ticked. The unit tests always run.

After a merge on GitHub, bring the forge's copy up to date:
`git pull origin main && git push forgejo main`.
