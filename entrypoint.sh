#!/command/with-contenv sh
set -eu

export DISPLAY="${DISPLAY:-:99}"
export NO_AT_BRIDGE=1
export SESSION_MANAGER=""

echo "Configuring hosts file for ad blocking..."
# Appended at runtime rather than baked into the image: Fly rewrites /etc/hosts on
# every boot (fly-local-6pn, instance ID, fly-global-services), so image content is lost.
if [ -f /app/hosts ]; then
  wc -l /app/hosts
  cat /app/hosts >> /etc/hosts
fi

