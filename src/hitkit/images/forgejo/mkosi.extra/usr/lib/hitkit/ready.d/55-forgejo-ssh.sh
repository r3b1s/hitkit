#!/bin/sh
# git over SSH: Forgejo's built-in server must be listening on 2222.
ss -ltn | grep -q ':2222 ' || { echo "forgejo ssh server not listening on 2222"; exit 1; }
