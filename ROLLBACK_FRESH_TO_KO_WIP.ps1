param([string]$Ip="192.168.0.25",[string]$Key="$env:USERPROFILE\.ssh\comma4_old_rsa")
$ErrorActionPreference="Stop"
ssh -i $Key -o IdentitiesOnly=yes "comma@$Ip" 'set -e; rm -rf /data/openpilot /data/openpilot.new /data/openpilot.old; rm -f /data/ko/door_can.json; git clone -b ko-wip --single-branch https://github.com/KeIIog/kopilot33.git /data/openpilot; cd /data/openpilot; echo KO_WIP_RESTORE_OK; sync; sudo reboot'
