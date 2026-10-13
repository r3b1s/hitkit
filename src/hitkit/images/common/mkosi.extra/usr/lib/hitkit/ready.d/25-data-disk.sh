#!/bin/sh
mountpoint -q /srv/data || { echo "data disk not mounted at /srv/data"; exit 1; }
