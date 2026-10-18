#!/bin/sh
# Run hitkit nested inside this job VM, bring up a forge, and run the e2e tests
# against it. The nested hitkit uses 10.78.0.0/24 so it cannot collide with the
# VM's own uplink, as in the hitkit-dev kits.
set -eu

uid=$(id -u)
step() { echo "::group::$*"; }
end() { echo "::endgroup::"; }

step "user session for the libvirt session daemon"
sudo loginctl enable-linger "$(id -un)"
for _ in $(seq 30); do [ -S "/run/user/$uid/bus" ] && break; sleep 1; done
export XDG_RUNTIME_DIR="/run/user/$uid"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
end

step "nested hitkit config"
mkdir -p "$HOME/.config/hitkit"
[ -f "$HOME/.config/hitkit/config.toml" ] || \
    printf 'subnet = "10.78.0.0/24"\ninstall_id = "%s"\n' "$(tr -d - < /proc/sys/kernel/random/uuid)" \
        > "$HOME/.config/hitkit/config.toml"
end

step "install"
uv sync --locked
hitkit=.venv/bin/hitkit
end

step "host setup"
# Docker sets the FORWARD policy to DROP, which would also drop the nested VMs' NAT
# traffic; this single-use VM does not need Docker.
if systemctl is-active --quiet docker.service; then
    sudo systemctl stop docker.socket docker.service
    sudo iptables -P FORWARD ACCEPT
fi
# The system libvirt daemon (hitkit's network); the image does not start it.
sudo systemctl start libvirtd.socket libvirtd-ro.socket
# Root must not leave bytecode in the runner's checkout, or the runner cannot clean it up.
sudo env PYTHONDONTWRITEBYTECODE=1 "$hitkit" setup
sudo env PYTHONDONTWRITEBYTECODE=1 "$hitkit" trust install
"$hitkit" doctor || true
end

status=0
step "hitkit up forge"
start=$(date +%s)
"$hitkit" up forge --memory runner=4096 --cpus runner=2 || status=$?
echo "up forge: exit $status after $(( $(date +%s) - start ))s"
end

if [ "$status" -eq 0 ]; then
    step "e2e tests"
    HITKIT_E2E=1 .venv/bin/python -m pytest tests/e2e/test_forge.py -v || status=$?
    end
fi

if [ "$status" -ne 0 ]; then
    step "trace of the latest failure"
    "$hitkit" trace --failed last || true
    end
fi
exit "$status"
