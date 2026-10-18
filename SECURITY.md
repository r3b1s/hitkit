# Security policy

## Reporting a vulnerability

Please do not report security vulnerabilities in public issues. Report them privately
by e-mail to r3b1s@myparse.win.

Include a description of the issue, the steps to reproduce it, the affected commit or
version and, if known, any mitigations.

You will get a reply within 7 days. If the vulnerability is confirmed, it is fixed on
`main` and published as a GitHub security advisory within 90 days of the report, or
sooner once a fix is available.

## Supported versions

hitkit has no releases yet. Security fixes are made on the `main` branch only.

## Scanning and remediation policy

Every pull request, every push to `main` and a regular schedule run the checks below. A
check that fails blocks merging; the branch ruleset on `main` requires them.

### Dependencies (OSV-Scanner)

OSV-Scanner checks every package in `uv.lock` against [OSV.dev](https://osv.dev), which
includes known vulnerabilities and OpenSSF's reports of malicious packages. It scans the
whole lock file, not only what a pull request changes, and runs daily as well.

- A **malicious package** is removed at once and is never ignored.
- A **known vulnerability** is fixed by updating or removing the dependency: within 7 days
  of being found if its severity is high or critical (CVSS 7.0 or more, or no score), and
  within 30 days otherwise.
- A vulnerability that cannot affect hitkit may instead be declared not exploitable, with
  an `[[IgnoredVulns]]` entry in `osv-scanner.toml` next to `uv.lock` giving the `id`, the
  `reason` and an `ignoreUntil` date no more than 90 days ahead, so the decision is
  reviewed.

Container images, apt packages and other artifacts pinned into the VM images are not
scanned yet.

### Code (CodeQL)

CodeQL analyses the Python code with the `security-extended` queries; it also runs
weekly. Findings appear under the repository's Security tab.

- Alerts with a security severity of high or critical, and error-level alerts, block
  merging. They are fixed before the change lands, or dismissed in GitHub as a false
  positive or "won't fix" with a comment saying why the code is not exploitable.
- Lower-severity alerts are fixed or dismissed with a reason within 30 days.

### Workflows (zizmor)

zizmor checks the GitHub Actions workflows. Any finding fails the check; it is fixed, or
suppressed with a `# zizmor: ignore[<rule>]` comment saying why.
