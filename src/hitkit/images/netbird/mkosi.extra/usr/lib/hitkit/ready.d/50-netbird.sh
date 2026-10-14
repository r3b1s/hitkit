#!/bin/sh
# Through Traefik on this machine (TLS, routing, embedded IdP), independent of host DNS.
nb="curl -fsS --max-time 5 -o /dev/null --resolve netbird.hitkit.internal:443:127.0.0.1"
$nb https://netbird.hitkit.internal/oauth2/.well-known/openid-configuration 2>&1 \
    || { systemctl is-failed --quiet netbird.service && echo "netbird.service failed"; echo "netbird server not serving over https"; exit 1; }
$nb https://netbird.hitkit.internal/ 2>&1 || { echo "netbird dashboard not serving over https"; exit 1; }
