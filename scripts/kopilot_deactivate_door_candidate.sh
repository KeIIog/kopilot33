#!/usr/bin/env bash
set -euo pipefail
rm -f /data/ko/door_can.json
cd /data/openpilot
python3 - <<'EOF'
from openpilot.common.params import Params
Params().put_bool("KoDoorControlEnabled", False)
print("KO door candidate removed and master toggle disabled")
EOF
