#!/bin/sh
for unit in proxmox-backup.service proxmox-backup-proxy.service; do
    systemctl is-active --quiet "$unit" || { echo "$unit not active"; exit 1; }
done
curl -fsSk --max-time 5 -o /dev/null https://127.0.0.1:8007/ || { echo "proxmox-backup-proxy not answering on 8007"; exit 1; }
