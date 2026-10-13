#!/bin/sh
# Job VMs and template builds are one-shot; only persistent runners run the daemon.
{ [ -e /run/hitkit/ephemeral ] || [ -e /run/hitkit/prepull ]; } && exit 0
systemctl is-active --quiet forgejo-runner.service || { echo "forgejo-runner not active"; exit 1; }
