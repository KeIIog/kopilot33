#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd /data/openpilot
python3 "$HERE/apply_kopilot_integrated_v1_6.py" --repo /data/openpilot --local
printf '\n%s\n' 'LOCAL_BUILD_OK. Rebooting into test...'
sync
sudo reboot
