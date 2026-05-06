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

if [ -f /app/hosts ]; then
  awk '
    NF >= 2 && $1 !~ /^#/ {
      for (i = 2; i <= NF; i++) {
        domain=tolower($i)
        gsub(/\r/, "", domain)
        sub(/\.$/, "", domain)
        if (domain == "" || domain == "localhost" || domain == "localhost.localdomain") {
          continue
        }
        if (domain ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) {
          continue
        }
        print domain
        print "*." domain
      }
    }
  ' /app/hosts > /home/user/tinyproxy-filter.txt
fi

wc -l /home/user/tinyproxy-filter.txt
