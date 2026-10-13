#!/bin/sh
# Names in the hitkit network must resolve via the network's dnsmasq. A failure
# here usually means a host firewall is dropping DNS on hitkit0.
getent ahostsv4 host.hitkit.internal >/dev/null 2>&1 \
    || { echo "cannot resolve host.hitkit.internal (host firewall blocking DNS on hitkit0?)"; exit 1; }
