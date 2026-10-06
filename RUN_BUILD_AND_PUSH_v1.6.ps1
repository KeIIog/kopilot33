param([string]$Repo="D:\kopilot33_KO_v1_2_builder\kopilot33-KO-FULL-v1.2\kopilot33-KO-FULL-v1.2")
$ErrorActionPreference="Stop"
$here=Split-Path -Parent $MyInvocation.MyCommand.Path
python "$here\apply_kopilot_integrated_v1_6.py" --repo "$Repo" --push
