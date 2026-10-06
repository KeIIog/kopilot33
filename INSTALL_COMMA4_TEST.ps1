param([string]$Ip="192.168.0.25",[string]$Key="$env:USERPROFILE\.ssh\comma4_old_rsa")
$ErrorActionPreference="Stop"
ssh -i $Key -o IdentitiesOnly=yes "comma@$Ip" 'set -e; rm -rf /data/openpilot /data/openpilot.new /data/openpilot.old; git clone -b test --single-branch https://github.com/KeIIog/kopilot33.git /data/openpilot; cd /data/openpilot; test -f KO_INTEGRATED_MANIFEST.json; test -x launch_openpilot.sh; echo TEST_INSTALL_OK; sync; sudo reboot'
