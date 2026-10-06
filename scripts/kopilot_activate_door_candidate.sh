#!/usr/bin/env bash
set -euo pipefail
cd /data/openpilot
mkdir -p /data/ko
cp -f KO_DOOR_CANDIDATE.json /data/ko/door_can.json
chmod 600 /data/ko/door_can.json
printf '%s
' 'Candidate installed: /data/ko/door_can.json'
printf '%s
' 'It is NOT enabled yet. In KO Web: Vehicle Aux -> KO 도어 제어 허용 = ON.'
printf '%s
' 'Test only with ignition ON, P, standstill, valid CAN, and openpilot disengaged.'
