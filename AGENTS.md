# Notes for agents working on hitkit

## Pinned versions

When adding or updating a pinned version (`pins.toml` artifacts, apt package
versions in `mkosi.conf`, container image tags and digests), pin the latest
stable release. There is no minimum release age: do not skip a release for
being new. Skip only prereleases (rc, beta, canary). Pin by exact version plus
its sha256 or image digest, as the existing pins do.
