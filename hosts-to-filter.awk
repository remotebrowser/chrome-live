# Rewrites an hblock hosts file into tinyproxy fnmatch patterns, emitting both
# the bare domain and "*.domain" so subdomains of a blocked entry match too.
# Bare IPs and localhost are skipped: tinyproxy matches on the request/CONNECT
# host, where neither ever appears as a target.
NF >= 2 && $1 !~ /^#/ {
  for (i = 2; i <= NF; i++) {
    domain = tolower($i)
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
