#!/usr/bin/env bash
# Deploy itermon.vectech.co (Firebase Hosting target "itermon").
#
# WHY THIS SCRIPT EXISTS -- read before "simplifying" it away.
# firebase.json's `public` is "docs", and Firebase uploads that directory FROM
# DISK. git's exclude rules mean nothing to it. docs/ on a working machine also
# holds this product's INTERNAL RECORDS -- worklogs, briefs, KPI reviews, the
# Web Store packet, verification evidence (~100 files). A plain
# `firebase deploy` would publish every one of them to a public website.
#
# firebase.json now carries ignore rules for those directories (defence in
# depth), but this script does not rely on them: it stages an explicit
# ALLOWLIST of the public site into site-build/ and deploys that. A new record
# directory added later is therefore excluded by default rather than included
# by default -- which is the only safe direction for this to fail in.
set -euo pipefail
cd "$(dirname "$0")"

STAGE="site-build"
ALLOW=(index.html admin.png bmc-qr.png)   # add new PUBLIC assets here, deliberately

rm -rf "$STAGE"; mkdir -p "$STAGE"
for f in "${ALLOW[@]}"; do
  if [ -e "docs/$f" ]; then cp -R "docs/$f" "$STAGE/"; else echo "missing: docs/$f" >&2; exit 1; fi
done

echo "--- staged for deploy ---"
find "$STAGE" -type f | sort
echo "--- record paths in the staging dir (MUST be empty) ---"
if find "$STAGE" -type d \( -name worklog -o -name briefs -o -name dispatches \
     -o -name distribution -o -name kpi-reviews -o -name verification -o -name spikes \) \
     | grep . ; then
  echo "ABORT: a record directory reached the staging dir." >&2; exit 1
fi
echo "(none)"

firebase deploy --config firebase.site.json --only hosting:itermon "$@"

rm -rf "$STAGE"
echo "deployed. Verify: curl -o /dev/null -w '%{http_code}\n' https://itermon.vectech.co/"
