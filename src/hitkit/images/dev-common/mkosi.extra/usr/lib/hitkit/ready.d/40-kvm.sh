#!/bin/sh
# Nested hitkit needs KVM inside this VM (host-passthrough CPU + nested=1 on the host).
[ -c /dev/kvm ] || { echo "/dev/kvm missing: nested virtualization unavailable"; exit 1; }
