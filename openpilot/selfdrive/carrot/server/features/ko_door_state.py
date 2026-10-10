"""Read-only Hyundai CAN-FD door-lock *indicator* inferred from field captures.

This is NOT an actuator protocol: do not use these bytes for CAN transmit.
The 0x411/0x414 bit interpretation is vehicle-specific and is not OEM verified.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterable

DOOR_STATUS_ADDRS = (0x411, 0x414)
DOOR_STATUS_BUS = 0


def decode_lock_indicator(address: int, payload: bytes) -> bool | None:
  """Return True locked, False unlocked, None unknown/transient."""
  if len(payload) != 8:
    return None
  if address == 0x411:
    # Captured on Ioniq 5: byte2 0x50/0x10, byte3 0x50/0x00.
    # During a transition the two fields can disagree for one frame.
    a = bool(payload[2] & 0x40)
    b = (payload[3] & 0x50) == 0x50
    if (payload[3] & 0x50) not in (0, 0x50) or a != b:
      return None
    return not a
  if address == 0x414:
    # byte4 0x2C unlocked, 0x0C locked in the same physical captures.
    return not bool(payload[4] & 0x20)
  return None


def infer_lock_state(frames: Iterable[tuple[float, int, int, bytes]], now: float) -> dict:
  """Confirm only matching, recent indications from *both* original bus-0 frames.

  frames: (monotonic_time, src, address, payload) tuples. Reject forwarded/TX.
  """
  by_id: dict[int, deque] = defaultdict(lambda: deque(maxlen=4))
  for ts, src, addr, data in frames:
    if src != DOOR_STATUS_BUS or addr not in DOOR_STATUS_ADDRS or now - ts > 0.85 or ts > now:
      continue
    val = decode_lock_indicator(addr, data)
    by_id[addr].append((ts, val))

  result = {
    "state": "unknown", "locked": None, "corroborated": False, "actuator_verified": False,
    "source": "inferred_ioniq5_canfd_0x411_0x414",
    "message": "Read-only status inference, not an OEM-confirmed lock signal",
    "frames_411": len(by_id[0x411]), "frames_414": len(by_id[0x414]),
  }
  readings = []
  for addr in DOOR_STATUS_ADDRS:
    seq = list(by_id[addr])
    if len(seq) < 2 or now - seq[-1][0] > 0.5:
      return result
    last2 = seq[-2:]
    if last2[0][1] is None or last2[0][1] != last2[1][1]:
      return result
    readings.append(last2[-1][1])
  if readings[0] != readings[1]:
    return result
  result["state"] = "locked" if readings[0] else "unlocked"
  result["locked"] = readings[0]
  result["corroborated"] = True  # Not a confirmation from the actuator.
  return result
