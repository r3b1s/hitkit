#!/bin/sh
docker info >/dev/null 2>&1 || { echo "docker daemon not responding"; exit 1; }
