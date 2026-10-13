#!/bin/sh
# Debian names the unit ssh.service, Arch sshd.service.
systemctl is-active --quiet ssh.service || systemctl is-active --quiet sshd.service \
    || { echo "sshd not active"; exit 1; }
