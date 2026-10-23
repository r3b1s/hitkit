#!/bin/sh
# Run hitkit nested inside this job VM, bring up kits, and run e2e tests against them.
#
#   nested-e2e.sh 'KIT [UP FLAGS]'... -- PYTEST ARGS
#
# Each quoted argument before `--` is one `hitkit up`, run in order; what follows `--`
# goes to pytest. The nested hitkit uses 10.78.0.0/24 so it cannot collide with the job
# VM's own uplink, as in the hitkit-dev kits. The job VM needs hitkit's host toolchain;
# see CONTRIBUTING.md, "Local CI".
set -eu

ups=""
while [ $# -gt 0 ] && [ "$1" != "--" ]; do
    ups="$ups$1
"
    shift
done
[ "${1:-}" = "--" ] && shift
[ -n "$ups" ] && [ $# -gt 0 ] || { echo "usage: $0 'KIT [UP FLAGS]'... -- PYTEST ARGS" >&2; exit 2; }

for tool in mkosi virsh qemu-system-x86_64 uv; do
    command -v "$tool" >/dev/null || {
        echo "$tool is missing: add hitkit's toolchain to ~/.config/hitkit/images/runner-arch.toml (CONTRIBUTING.md, Local CI)" >&2
        exit 1
    }
done

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
while IFS= read -r up && [ "$status" -eq 0 ]; do
    [ -n "$up" ] || continue
    step "hitkit up $up"
    start=$(date +%s)
    # shellcheck disable=SC2086 # word splitting turns the spec into arguments
    # stdin from /dev/null: ssh in the kits' hooks would otherwise eat the remaining ups.
    "$hitkit" up $up </dev/null || status=$?
    echo "up ${up%% *}: exit $status after $(( $(date +%s) - start ))s"
    end
done <<UPS
$ups
UPS

if [ "$status" -eq 0 ]; then
    step "e2e tests"
    # Every kit a job tests is up by now, so a test that finds its kit missing is a failure.
    HITKIT_E2E=1 HITKIT_E2E_REQUIRE=1 .venv/bin/python -m pytest "$@" -v -rs || status=$?
    end
fi

if [ "$status" -ne 0 ]; then
    step "trace of the latest failure"
    "$hitkit" trace --failed last || true
    end
fi
exit "$status"
