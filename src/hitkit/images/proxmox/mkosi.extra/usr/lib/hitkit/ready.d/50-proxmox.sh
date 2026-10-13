#!/bin/sh
systemctl is-active --quiet pve-cluster.service || { echo "pve-cluster not active"; exit 1; }
curl -fsSk --max-time 5 -o /dev/null https://127.0.0.1:8006/ || { echo "pveproxy not answering on 8006"; exit 1; }
