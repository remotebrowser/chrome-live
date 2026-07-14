#!/bin/sh
set -eu

# s6-overlay refuses to run unless it is PID 1. The host runtime decides whether
# the image ENTRYPOINT gets to be PID 1:
#
#   - Plain Docker / Podman make the ENTRYPOINT PID 1, so `exec /init` puts
#     s6-svscan in the PID 1 slot directly. `unshare --pid` is blocked here
#     without extra privileges, so the direct path is the only one that works.
#
#   - Fly.io and Daytona inject their own init/daemon as PID 1 and run the
#     ENTRYPOINT as a child. The sandbox still has CAP_SYS_ADMIN, so we move s6
#     into a nested PID namespace via unshare, where /init becomes PID 1.
#
# Three independent signals trigger the unshare path:
#   * Fly sets FLY_* env vars on its machines even when the ENTRYPOINT happens
#     to be PID 1, so honor them explicitly (preserves the original Fly path).
#   * Daytona sets DAYTONA_WS_DIR / DAYTONA_WORKSPACE_ID (or DAYTONA=true) in
#     its snapshots/sandboxes, so honor them explicitly as well.
#   * Otherwise, our own PID not being 1 means a runtime is sitting above us
#     (Daytona does this; it makes the entrypoint a child of its daemon), so the
#     nested PID namespace is required.
if [ -n "${FLY_APP_NAME:-}${FLY_MACHINE_ID:-}${FLY_ALLOC_ID:-}" ]; then
  exec /usr/bin/unshare --pid --fork --mount-proc /init
fi

if [ -n "${DAYTONA_WS_DIR:-}${DAYTONA_WORKSPACE_ID:-}" ] || [ "${DAYTONA:-}" = "true" ]; then
  exec /usr/bin/unshare --pid --fork --mount-proc /init
fi

if [ "$$" -ne 1 ]; then
  exec /usr/bin/unshare --pid --fork --mount-proc /init
fi

exec /init