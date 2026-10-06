#!/usr/bin/env bash
set -euo pipefail
cd /data/openpilot
rm -f /data/ko/door_can.json
python3 - <<'PY'
from openpilot.common.params import Params
Params().put_bool("KoDoorControlEnabled", False)
PY
git switch -f ko-wip
sync
sudo reboot
