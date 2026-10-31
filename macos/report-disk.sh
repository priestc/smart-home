#!/bin/sh
# Reports this Mac's internal drive usage to the smart-home dashboard Storage widget.
# Run every 5 minutes by the launchd agent that install-disk-report.sh sets up.
set -eu

API="${SMART_HOME_API:-http://tank2:5000}"
LABEL="${SMART_HOME_DISK_LABEL:-Mac mini}"

# The Data volume's Available is the free space of the whole APFS container, so
# used = total - free covers the system volume, snapshots and everything else.
set -- $(df -k /System/Volumes/Data | tail -1)
total=$(($2 * 1024))
free=$(($4 * 1024))

curl -fsS --max-time 20 \
  --data-urlencode "label=$LABEL" -d "total=$total" -d "free=$free" \
  "$API/api/disks/report"
