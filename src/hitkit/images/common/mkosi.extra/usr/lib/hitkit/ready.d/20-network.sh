#!/bin/sh
# The static address from the hitkit.network credential must be configured.
want=$(sed -n 's/^Address=\(.*\)\/.*/\1/p' "$CREDENTIALS_DIRECTORY/hitkit.network")
ip -4 -o addr show | grep -q " $want/" || { echo "address $want not configured"; exit 1; }
