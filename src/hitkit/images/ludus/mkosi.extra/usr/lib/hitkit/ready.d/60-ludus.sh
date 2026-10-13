#!/bin/sh
# Ludus installed, and its API answering over verified TLS (the hitkit CA leaf).
if [ ! -f /opt/ludus/install/.stage-3-complete ]; then
    if systemctl is-failed --quiet hitkit-ludus-install.service; then
        echo "Ludus install failed: journalctl -u hitkit-ludus-install; /opt/ludus/install/install.log"
    else
        echo "Ludus install not finished"
    fi
    exit 1
fi
# Any HTTP answer over verified TLS will do: the API rejects unauthenticated calls (401).
code=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "https://$(hostname).hitkit.internal:8080/api/v2/") || code=000
case "$code" in
    2??|401) ;;
    *) echo "Ludus API not answering on :8080 (HTTP $code)"; exit 1 ;;
esac
