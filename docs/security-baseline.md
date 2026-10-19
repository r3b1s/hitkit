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

Why not a higher level: one maintainer (level 2 needs two). The
privileged parts of hitkit (`sudo hitkit setup`, `hitkit trust install`, credential
generation) are a reason for closer review, not for a higher level.

## Constraints

- **Hosting:** <https://github.com/r3b1s/hitkit> is the canonical, public repository.
  A GitLab copy exists and is private.
- **CI:** GitHub Actions on GitHub-hosted runners only. No image builds or end-to-end
  tests there: they need KVM, root for host setup and image builds. They run in the
  maintainer's local CI on a hitkit forge (`.forgejo/`, CONTRIBUTING.md "Local CI")
  before a change is pushed to GitHub. Self-hosted runners must never run pull requests
  from this public repository.
- **Releases:** git tags (`v0.1.0` is the first), installed from a checkout of the tag.
  The only release assets are the source archives GitHub generates for each tag; nothing
  is published to a package registry.
- **Tools chosen:** OSV-Scanner (SCA), CodeQL (SAST), zizmor (workflow analysis), OSPS
  Baseline scanner action (`revanite-io/osps-baseline-action`). Every action and tool is
  pinned to its latest stable release by exact version and commit SHA or sha256, per
  [AGENTS.md](../AGENTS.md).
- **Scope of dependency scanning:** `uv.lock` only (see open items).

## Standing permissions

None. Agents propose every change and make it only once the maintainer approves.

## Results

Checked 2026-10-18 by reading the repository and the GitHub API, unless noted. hitkit's
first release is `v0.1.0`, so the "after a release" requirements apply.

| Requirement | Status | How it was checked |
|---|---|---|
| OSPS-AC-01.01 MFA for sensitive actions | met | self-reported by the maintainer: 2FA is on for the only account with access |
| OSPS-AC-02.01 lowest default permissions | met | personal-account repository; self-reported: no collaborators. Any collaborator would get write access, the only role GitHub offers here |
| OSPS-AC-03.01 no direct commits to `main` | met | ruleset `protection` (id 25358796) targets the default branch and requires a pull request, the three CI checks and CodeQL results (`GET /repos/r3b1s/hitkit/rules/branches/main`). Its bypass list needs admin access to read and was not checked. The OSPS Baseline scanner reports this as failed; see open items |
| OSPS-AC-03.02 `main` cannot be deleted | met | the same ruleset restricts deletion and blocks force pushes; the OSPS Baseline scanner agrees |
| OSPS-BR-01.01 untrusted metadata in pipelines | met | workflows read event data only through action inputs, never in `run:` scripts; `zizmor 1.31.1` reports no findings |
| OSPS-BR-01.03 untrusted code kept from privileged credentials | met | no `pull_request_target` or `workflow_run` triggers; the only secret (`PVTR_GITHUB_TOKEN`) is used by `osps-baseline.yml`, which runs only on schedule and manual dispatch |
| OSPS-BR-03.01 encrypted official channels | met | the only official channel is the GitHub repository (HTTPS/SSH) |
| OSPS-BR-03.02 authenticated distribution | met | installed from a `git clone` over HTTPS/SSH; nothing is published elsewhere |
| OSPS-BR-07.01 secrets kept out of version control | met | `.gitignore` covers likely secret files; hitkit stores generated secrets in XDG directories outside the checkout; the OSPS Baseline scanner reports GitHub secret scanning and push protection both enabled. A pattern grep of the full history found only test fixtures, not a full secret scan |
| OSPS-DO-01.01 user guides | met | the README covers install, host setup and the use of every kit; compared with `hitkit --help`, every command except `image` and `pool` is shown there (both are in docs/design.md; `pool run` is run by a service, not by users). Destructive commands need `--yes`, and the README says data is kept unless deleted explicitly; the OSPS Baseline scanner disagrees, see open items |
| OSPS-DO-02.01 defect reporting guide | met | README and CONTRIBUTING.md point to GitHub issues and say what a report contains |
| OSPS-GV-02.01 public discussion | met | GitHub issues, linked from the README |
| OSPS-GV-03.01 contribution guide | met | CONTRIBUTING.md |
| OSPS-LE-02.01 open source licence | met | Apache-2.0 (OSI approved) |
| OSPS-LE-02.02 licence of released assets | met | the `v0.1.0` source archive (`archive/refs/tags/v0.1.0.tar.gz`) is the only release asset, under Apache-2.0 |
| OSPS-LE-03.01 licence in a standard location | met | `LICENSE`, matching `license = "Apache-2.0"` in `pyproject.toml`; a built wheel carries `License-Expression: Apache-2.0` |
| OSPS-LE-03.02 licence with released assets | met | the `v0.1.0` source archive contains `LICENSE`, identical to the Apache-2.0 text (sha256 `cfc7749b…`) |
| OSPS-QA-01.01 public source at a stable URL | met | the repository opens without signing in |
| OSPS-QA-01.02 public change history | met | full history is public |
| OSPS-QA-02.01 dependency list | met | `pyproject.toml` and `uv.lock`; the third-party imports in `src/` are exactly click, cryptography and libvirt |
| OSPS-QA-04.01 list of codebases | not applicable | single repository |
| OSPS-QA-05.01 no generated executables | met | `git log --all --numstat` lists one binary file in history: `proxmox.gpg` |
| OSPS-QA-05.02 no unreviewable binaries | met | that file is a public OpenPGP key (`file`), not code |
| OSPS-VM-02.01 security contacts | met | SECURITY.md |
| OSPS-QA-06.01 tests in CI | met | `ci.yml` runs `uv run pytest` on every pull request; it ran and passed on [#1](https://github.com/r3b1s/hitkit/pull/1) and on `main`, and *Unit tests* is a required check in the ruleset |
| OSPS-QA-06.02 test documentation | met | CONTRIBUTING.md: local unit and end-to-end tests, the CI checks, and how to read results |
| OSPS-VM-05.01 SCA remediation policy | met | SECURITY.md, "Scanning and remediation policy". Licence findings are not covered: hitkit has three direct dependencies, so licences are reviewed when one is added |
| OSPS-VM-05.03 SCA check on every change | met | `osv-scanner.yml` scans all of `uv.lock` on every pull request; it passed on [#1](https://github.com/r3b1s/hitkit/pull/1) and *Dependency scan (OSV-Scanner) / osv-scan* is a required check. OSV-Scanner 2.7.0 run locally flags a known-malicious PyPI package (MAL-2023-1401) in a test file |
| OSPS-VM-06.01 SAST remediation policy | met | SECURITY.md, "Scanning and remediation policy" |
| OSPS-VM-06.02 SAST check on every change | met | `codeql.yml` runs on every pull request; it passed on [#1](https://github.com/r3b1s/hitkit/pull/1) with no alerts, and the ruleset's code scanning rule blocks merging on CodeQL alerts of high or higher security severity and on errors |

## Accepted risks

The design's threat model, [docs/design.md §5](design.md#5-threat-model), lists the
risks hitkit accepts by design (QEMU escape into the user's account, host secrets
unencrypted at rest, long-lived persistent runners, and others). No further risks have
been accepted in this assessment.

## Open items

- **OSPS Baseline scanner disagrees on two controls.** In its first run
  ([39170950827](https://github.com/r3b1s/hitkit/actions/runs/39170950827), scanner
  v0.31.0, catalog `osps-baseline-2026-08`) every other level 1 control passed.
  - OSPS-AC-03.01: the scanner only accepts a "Restrict updates" rule, a pull request
    rule with at least one required approval, or classic protection that restricts
    pushes or requires approvals (`evaluation_plans/osps/access_control/steps.go`). A
    required pull request with no approvals is not on that list, yet it prevents direct
    commits, which is all the requirement asks. Changing the ruleset to satisfy the
    scanner would need a bypass for the only maintainer, which weakens it, so the
    ruleset stays as it is. Decided 2026-10-18.
  - OSPS-DO-01.01: the scanner looks for a user guide only in a Security Insights file.
    The README is the user guide (see results); a Security Insights file pointing to it
    would satisfy the scanner. Not added.
- **Not scanned for vulnerabilities:** container images, apt packages and other artifacts
  pinned into the VM images (`pins.toml`, `mkosi.conf`).
- **No full secret scan of history** has been run with a dedicated scanner.
- **README security model.** The README does not yet summarise the risks of running
  hitkit (sudo setup, the host CA, where credentials live) or point to the threat model.
- **Keeping pins current.** Nothing proposes updates to the pinned actions and tools yet;
  pins were last brought up to date by hand for `v0.1.0`.
- **Highly visible warnings.** DO-01.01's recommendation asks for highly visible warnings
  on destructive actions. `destroy --delete-data`, `gc` and `restore` are documented with
  `--yes` and comments, not with a warning callout.
