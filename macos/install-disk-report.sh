#!/bin/sh
# Installs a launchd agent that reports this Mac's drive usage to the dashboard
# every 5 minutes. The script is copied out of the repo because launchd agents
# can't read ~/Documents without Full Disk Access. Re-run after editing report-disk.sh.
set -eu

NAME=com.smart-home.report-disk
DEST="$HOME/.local/bin/smart-home-report-disk"
PLIST="$HOME/Library/LaunchAgents/$NAME.plist"
LOG="$HOME/Library/Logs/smart-home-report-disk.log"

mkdir -p "$(dirname "$DEST")"
install -m 755 "$(dirname "$0")/report-disk.sh" "$DEST"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$NAME</string>
  <key>ProgramArguments</key><array><string>$DEST</string></array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$NAME" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $NAME; logs in $LOG"
