# KOPilot KO-v1.2

Base repository: https://github.com/ajouatom/openpilot.git
Branch: carrot-wip
Frozen SHA: e6a6284437a76c2d60bda75efa4cb2cf6e85ef07

Runtime compatibility: the active import path remains openpilot.selfdrive.carrot to avoid breaking current upstream dependencies. openpilot/selfdrive/ko is a patched compatibility snapshot.

Not guessed/reapplied automatically:
- exact old huge DriverMonitoring2.INTERACTION_TIMEOUTS tuple (numeric values unavailable)
- exact old +0.80 m/s^3 positive-acceleration smoothing diff (implementation location/diff unavailable)
