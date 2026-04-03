#!/bin/sh
set -eu

# Fly.io uses its own PID 1, so s6-overlay must run in a nested PID namespace.
# Regular Docker usually blocks unshare without extra privileges, so use /init directly.
if [ -n "${FLY_APP_NAME:-}" ] || [ -n "${FLY_MACHINE_ID:-}" ] || [ -n "${FLY_ALLOC_ID:-}" ]; then
  exec /usr/bin/unshare --pid --fork --mount-proc /init
fi

exec /init
