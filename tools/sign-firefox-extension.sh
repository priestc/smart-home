#!/usr/bin/env bash
# Sign chrome_extension/ with Mozilla as an unlisted (private) add-on, so it
# stays installed in regular Firefox. Prints where the signed .xpi was saved.
#
# Needs AMO API credentials, kept out of the repo in ~/.config/web-ext/amo.env:
#   export WEB_EXT_API_KEY='user:12345:678'
#   export WEB_EXT_API_SECRET='...'
# (generate them at https://addons.mozilla.org/developers/addon/api/key/)
#
# Mozilla accepts each version only once: bump "version" in
# chrome_extension/manifest.json before signing a changed extension.
set -euo pipefail
cd "$(dirname "$0")/.."
KEYS="${AMO_KEYS_FILE:-$HOME/.config/web-ext/amo.env}"
if [ ! -f "$KEYS" ]; then
  echo "No AMO credentials at $KEYS (see the top of this script)." >&2
  exit 1
fi
if [ "$(stat -f %Lp "$KEYS" 2>/dev/null || stat -c %a "$KEYS")" != "600" ]; then
  chmod 600 "$KEYS"
fi
# shellcheck disable=SC1090
source "$KEYS"
: "${WEB_EXT_API_KEY:?WEB_EXT_API_KEY isn't set in $KEYS}"
: "${WEB_EXT_API_SECRET:?WEB_EXT_API_SECRET isn't set in $KEYS}"
VERSION=$(python3 -c "import json; print(json.load(open('chrome_extension/manifest.json'))['version'])")
echo "Signing version $VERSION (unlisted)..."
npx --yes web-ext@latest sign \
  --source-dir chrome_extension \
  --ignore-files README.md \
  --channel unlisted \
  --artifacts-dir web-ext-artifacts
echo
echo "Signed file(s):"
ls -1 web-ext-artifacts/*"$VERSION"*.xpi 2>/dev/null || ls -1t web-ext-artifacts/*.xpi | head -1
