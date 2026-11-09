#!/command/with-contenv sh
set -eu

SRC=/opt/capsolver
EXT_DIR=/home/user/capsolver
CRX=/home/user/capsolver.crx
# Google Chrome on Linux installs any .crx listed in this directory (external
# extensions). Branded Chrome ignores --load-extension since v137, so this is
# the only way to side-load there; custom/cloak use --load-extension on EXT_DIR.
EXTERNAL_DIR=/usr/share/google-chrome/extensions

rm -rf "$EXT_DIR" "$CRX" "$EXTERNAL_DIR"/*.json

if [ -z "${CAPSOLVER_API_KEY:-}" ]; then
  echo "capsolver: CAPSOLVER_API_KEY not set, extension disabled"
  exit 0
fi

case "$CAPSOLVER_API_KEY" in
  *[!A-Za-z0-9_-]*)
    echo "capsolver: CAPSOLVER_API_KEY has unexpected characters, extension disabled" >&2
    exit 0
    ;;
esac

cp -r "$SRC" "$EXT_DIR"
sed -i "s|apiKey: ''|apiKey: '${CAPSOLVER_API_KEY}'|" "$EXT_DIR/assets/config.js"
grep -q "apiKey: '${CAPSOLVER_API_KEY}'" "$EXT_DIR/assets/config.js"

google-chrome-stable --no-sandbox --pack-extension="$EXT_DIR" --pack-extension-key=/opt/capsolver.pem >/dev/null 2>&1
test -s "$CRX"
chown -R user:user "$EXT_DIR" "$CRX"

ID=$(openssl rsa -in /opt/capsolver.pem -pubout -outform DER 2>/dev/null | sha256sum | head -c 32 | tr 0-9a-f a-p)
VERSION=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$EXT_DIR/manifest.json")
mkdir -p "$EXTERNAL_DIR"
printf '{"external_crx": "%s", "external_version": "%s"}\n' "$CRX" "$VERSION" > "$EXTERNAL_DIR/$ID.json"

echo "capsolver: extension $ID v$VERSION enabled"
