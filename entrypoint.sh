#!/command/with-contenv sh
set -eu

export DISPLAY="${DISPLAY:-:99}"
export NO_AT_BRIDGE=1
export SESSION_MANAGER=""

echo "Configuring hosts file for ad blocking..."
if [ -f /app/hosts ]; then
  wc -l /app/hosts
  cat /app/hosts >> /etc/hosts
fi
