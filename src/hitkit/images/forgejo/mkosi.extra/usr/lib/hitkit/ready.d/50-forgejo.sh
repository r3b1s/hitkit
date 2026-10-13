#!/bin/sh
curl -fsS --max-time 5 https://forgejo.hitkit.internal/api/healthz >/dev/null 2>&1 \
    || { systemctl is-failed --quiet forgejo-bootstrap.service && echo "forgejo-bootstrap failed"; echo "forgejo not healthy over https"; exit 1; }
