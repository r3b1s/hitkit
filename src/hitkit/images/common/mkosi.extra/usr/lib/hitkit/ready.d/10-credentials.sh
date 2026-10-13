#!/bin/sh
systemctl is-active --quiet hitkit-provision.service || { echo "hitkit-provision.service failed"; exit 1; }
