# Security baseline: project record

Where hitkit stands against the [OpenSSF OSPS Baseline](https://baseline.openssf.org/).
Statuses describe the commit this file ships with; a re-audit starts from here.

## Baseline

- **Version:** [v2026.08.28](https://baseline.openssf.org/versions/2026-08-28)
- **Last assessed:** 2026-10-18

## Level

**Level 1 plus** these higher-level requirements:

| Requirement | Level | Why |
|---|---|---|
| OSPS-QA-06.01: tests run in CI before a change is accepted | 2 | needed for QA-06.02, and a cheap safeguard |
| OSPS-QA-06.02: when and how tests run is documented | 3 | contributors should know what CI checks and what stays local |
| OSPS-VM-05.01: remediation policy for dependency findings | 3 | the policy VM-05.03 enforces |
| OSPS-VM-05.03: every change checked for vulnerable and malicious dependencies | 3 | continuous awareness of dependency vulnerabilities |
| OSPS-VM-06.01: remediation policy for SAST findings | 3 | the policy VM-06.02 enforces |
| OSPS-VM-06.02: every change checked by SAST | 3 | hitkit's own code runs privileged host setup and builds commands |

Why not a higher level: one maintainer (level 2 needs two) and no releases yet. The
privileged parts of hitkit (`sudo hitkit setup`, `hitkit trust install`, credential
generation) are a reason for closer review, not for a higher level.

## Constraints

- **Hosting:** <https://github.com/r3b1s/hitkit> is the canonical, public repository.
  A GitLab copy exists and is private.
- **CI:** GitHub Actions on GitHub-hosted runners only. No image builds or end-to-end
  tests in CI; those run locally (they need KVM, root for host setup and long image
  builds). Self-hosted runners must never run pull requests from this public repository.
- **Tools chosen:** OSV-Scanner (SCA), CodeQL (SAST), zizmor (workflow analysis), OSPS
  Baseline scanner action (`revanite-io/osps-baseline-action`). Every action and tool is
  pinned to its latest stable release by exact version and commit SHA or sha256, per
  [AGENTS.md](../AGENTS.md).
- **Scope of dependency scanning:** `uv.lock` only (see open items).

## Standing permissions

None. Agents propose every change and make it only once the maintainer approves.

## Results

Checked 2026-10-18 by reading the repository and the GitHub API, unless noted. "After a
release" requirements are not applicable until hitkit makes its first release.

| Requirement | Status | How it was checked |
|---|---|---|
| OSPS-AC-01.01 MFA for sensitive actions | met | self-reported by the maintainer: 2FA is on for the only account with access |
| OSPS-AC-02.01 lowest default permissions | met | personal-account repository; self-reported: no collaborators. Any collaborator would get write access, the only role GitHub offers here |
| OSPS-AC-03.01 no direct commits to `main` | **not met** | ruleset `protection` requires a pull request, but its target list is empty, so it applies to no branch (`GET /repos/r3b1s/hitkit/rulesets/25358796`, `rules/branches/main` returns none) |
| OSPS-AC-03.02 `main` cannot be deleted | **not met** | same ruleset, same empty target list |
| OSPS-BR-01.01 untrusted metadata in pipelines | met | workflows read event data only through action inputs, never in `run:` scripts; `zizmor 1.31.1` reports no findings |
| OSPS-BR-01.03 untrusted code kept from privileged credentials | met | no `pull_request_target` or `workflow_run` triggers; the only secret (`PVTR_GITHUB_TOKEN`) is used by `osps-baseline.yml`, which runs only on schedule and manual dispatch |
| OSPS-BR-03.01 encrypted official channels | met | the only official channel is the GitHub repository (HTTPS/SSH) |
| OSPS-BR-03.02 authenticated distribution | met | installed from a `git clone` over HTTPS/SSH; nothing is published elsewhere |
| OSPS-BR-07.01 secrets kept out of version control | met | `.gitignore` covers likely secret files; hitkit stores generated secrets in XDG directories outside the checkout; GitHub secret scanning is on for public repositories. A pattern grep of the full history found only test fixtures, not a full secret scan |
| OSPS-DO-01.01 user guides | not applicable (after a release) | the README already documents install and every command; destructive commands need `--yes` |
| OSPS-DO-02.01 defect reporting guide | met ahead of a release | README and CONTRIBUTING.md point to GitHub issues and say what a report contains |
| OSPS-GV-02.01 public discussion | met | GitHub issues, linked from the README |
| OSPS-GV-03.01 contribution guide | met | CONTRIBUTING.md |
| OSPS-LE-02.01 open source licence | met | Apache-2.0 (OSI approved) |
| OSPS-LE-02.02 licence of released assets | not applicable | no releases |
| OSPS-LE-03.01 licence in a standard location | met | `LICENSE`, matching `license = "Apache-2.0"` in `pyproject.toml`; a built wheel carries `License-Expression: Apache-2.0` |
| OSPS-LE-03.02 licence with released assets | not applicable | no releases |
| OSPS-QA-01.01 public source at a stable URL | met | the repository opens without signing in |
| OSPS-QA-01.02 public change history | met | full history is public |
| OSPS-QA-02.01 dependency list | met | `pyproject.toml` and `uv.lock`; the third-party imports in `src/` are exactly click, cryptography and libvirt |
| OSPS-QA-04.01 list of codebases | not applicable | single repository |
| OSPS-QA-05.01 no generated executables | met | `git log --all --numstat` lists one binary file in history: `proxmox.gpg` |
| OSPS-QA-05.02 no unreviewable binaries | met | that file is a public OpenPGP key (`file`), not code |
| OSPS-VM-02.01 security contacts | met | SECURITY.md |
| OSPS-QA-06.01 tests in CI | **not checked** | `ci.yml` runs `uv run pytest` on every pull request; not yet seen running on GitHub, and not yet a required check |
| OSPS-QA-06.02 test documentation | met | CONTRIBUTING.md: local unit and end-to-end tests, the CI checks, and how to read results |
| OSPS-VM-05.01 SCA remediation policy | met | SECURITY.md, "Scanning and remediation policy". Licence findings are not covered: hitkit has three direct dependencies, so licences are reviewed when one is added |
| OSPS-VM-05.03 SCA check on every change | **not checked** | `osv-scanner.yml` scans all of `uv.lock` on every pull request; OSV-Scanner 2.7.0 run locally finds no issues and does flag a known-malicious PyPI package (MAL-2023-1401) in a test file. Not yet a required check |
| OSPS-VM-06.01 SAST remediation policy | met | SECURITY.md, "Scanning and remediation policy" |
| OSPS-VM-06.02 SAST check on every change | **not checked** | `codeql.yml` runs on every pull request; the ruleset blocks on CodeQL high-or-higher and error alerts, but applies to no branch yet, and CodeQL has not run yet |

## Accepted risks

The design's threat model, [docs/design.md §5](design.md#5-threat-model), lists the
risks hitkit accepts by design (QEMU escape into the user's account, host secrets
unencrypted at rest, long-lived persistent runners, and others). No further risks have
been accepted in this assessment.

## Open items

- **Ruleset targets no branch.** Add the default branch as its target, then add the
  required status checks *Unit tests*, *Workflow lint (zizmor)* and the OSV-Scanner job
  (shown as *Dependency scan (OSV-Scanner) / osv-scan*) once they have run once. Closes AC-03.01, AC-03.02, and with CI,
  QA-06.01, VM-05.03 and VM-06.02.
- **OSPS Baseline scanner secret.** `osps-baseline.yml` needs a `PVTR_GITHUB_TOKEN`
  repository secret (a read-only personal access token).
- **Not scanned for vulnerabilities:** container images, apt packages and other artifacts
  pinned into the VM images (`pins.toml`, `mkosi.conf`).
- **No full secret scan of history** has been run with a dedicated scanner.
- **README security model.** The README does not yet summarise the risks of running
  hitkit (sudo setup, the host CA, where credentials live) or point to the threat model.
- **Keeping pins current.** Nothing proposes updates to the pinned actions and tools yet.
- **First release.** DO-01.01, LE-02.02 and LE-03.02 apply then.
