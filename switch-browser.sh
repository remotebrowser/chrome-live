#!/bin/sh
# switch-browser chrome|cloak|custom
#
# Switches the active browser at runtime: writes the choice to the state file
# read by the chromium s6 service, then restarts that service. The new browser
# comes up on the same CDP port (:9221), so browser-trace and cdp-proxy keep
# working. Reachable over ssh, or wire it to browser-trace.
set -eu

STATE_FILE=/home/user/.active-browser
SERVICE=/run/service/chromium

usage() {
  echo "usage: switch-browser chrome|cloak|custom" >&2
  exit 2
}

[ $# -eq 1 ] || usage

case "$1" in
  chrome) ;;
  cloak)
    command -v cloak-browser >/dev/null 2>&1 || {
      echo "switch-browser: CloakBrowser not available (amd64 only)" >&2
      exit 1
    }
    ;;
  custom)
    command -v custom-chrome >/dev/null 2>&1 || {
      echo "switch-browser: custom-chrome not available" >&2
      exit 1
    }
    ;;
  *) usage ;;
esac

echo "$1" > "$STATE_FILE"
echo "active browser -> $1; restarting chromium service"
/command/s6-svc -r "$SERVICE"
