#!/bin/bash
# Build the iOS app and upload it to TestFlight.
#
#   bash ios/testflight.sh
#
# Signing is automatic (team 6V798W6LMZ). To upload, xcodebuild needs App Store
# Connect access, either:
#   - an App Store Connect API key (preferred; works without Xcode open):
#     put AuthKey_<KEY_ID>.p8 in ~/.appstoreconnect/private_keys/ and create
#     ~/.appstoreconnect/smart-home.env containing
#         ASC_KEY_ID=<KEY_ID>
#         ASC_ISSUER_ID=<issuer UUID>
#   - or, without that file, the Apple account signed in to Xcode
#     (Xcode > Settings > Accounts).
#
# The build number is the current date and time, so every upload is newer
# than the last. The version (1.0) comes from the Xcode project.
set -euo pipefail

cd "$(dirname "$0")/SmartHomeNotify"
OUT="${TMPDIR:-/tmp}/smart-home-testflight"
BUILD="$(date +%Y%m%d.%H%M)"
TEAM="6V798W6LMZ"

AUTH=()
ENV_FILE="$HOME/.appstoreconnect/smart-home.env"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    KEY="$HOME/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID}.p8"
    if [[ ! -f "$KEY" ]]; then
        echo "error: $ENV_FILE names key $ASC_KEY_ID but $KEY doesn't exist" >&2
        exit 1
    fi
    AUTH=(-authenticationKeyPath "$KEY" -authenticationKeyID "$ASC_KEY_ID" -authenticationKeyIssuerID "$ASC_ISSUER_ID")
    echo "Using App Store Connect API key $ASC_KEY_ID"
else
    echo "No $ENV_FILE; using the Apple account signed in to Xcode"
fi

rm -rf "$OUT"
mkdir -p "$OUT"
cat > "$OUT/ExportOptions.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key><string>app-store-connect</string>
    <key>destination</key><string>upload</string>
    <key>teamID</key><string>$TEAM</string>
    <key>signingStyle</key><string>automatic</string>
    <key>uploadSymbols</key><true/>
    <key>manageAppVersionAndBuildNumber</key><false/>
</dict>
</plist>
EOF

echo "Archiving build $BUILD..."
xcodebuild -project SmartHomeNotify.xcodeproj -scheme SmartHomeNotify -configuration Release \
    -destination 'generic/platform=iOS' -archivePath "$OUT/SmartHomeNotify.xcarchive" \
    -allowProvisioningUpdates ${AUTH[@]+"${AUTH[@]}"} \
    CURRENT_PROJECT_VERSION="$BUILD" \
    archive | grep -E "error:|warning: .*(sign|provision)|ARCHIVE (SUCCEEDED|FAILED)" || true
[[ -d "$OUT/SmartHomeNotify.xcarchive" ]] || { echo "error: archive failed (run without the grep filter to see why)" >&2; exit 1; }

echo "Uploading to App Store Connect..."
xcodebuild -exportArchive -archivePath "$OUT/SmartHomeNotify.xcarchive" \
    -exportOptionsPlist "$OUT/ExportOptions.plist" -exportPath "$OUT/export" \
    -allowProvisioningUpdates ${AUTH[@]+"${AUTH[@]}"}

echo "Uploaded build $BUILD. It shows up in TestFlight after Apple finishes processing it (usually 5-15 minutes)."
