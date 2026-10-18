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
kits are up. Run them before changing images, kits or the machine layer.

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

The end-to-end tests do not run in CI, by choice: they need KVM, root for host setup and
long image builds, so they run locally as described above.

A weekly workflow, `osps-baseline.yml`, also assesses the repository against the OSPS
Baseline; it never runs on pull requests.
