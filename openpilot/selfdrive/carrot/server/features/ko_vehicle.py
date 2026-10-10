from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from openpilot.cereal import car, messaging
from openpilot.common.params import Params
from openpilot.selfdrive.pandad import can_list_to_can_capnp
from .ko_door_state import infer_lock_state

DOOR_LOG_PATH = Path(os.environ.get("KO_DOOR_CONTROL_LOG", "/data/ko/door_control.jsonl"))
DOOR_COUNTER_PATH = Path(os.environ.get("KO_DOOR_COUNTER", "/data/ko/door_counter.json"))
DOOR_ADDR = 0x3FF
DOOR_BUS = 0
DOOR_SAFETY_FLAG = 4096
DOOR_PROTOCOL = "hyundai-canfd-0x3ff-v1"

# Last physical capture ended at rolling counter 0xA. The first generated
# command therefore starts at 0xB unless a stored/explicit value overrides it.
DOOR_COUNTER_DEFAULT = 0xA
_DOOR_SEND_SOCK = None
_DOOR_COMMAND_LOCK = asyncio.Lock()


def _live_state() -> dict[str, Any]:
  sm = messaging.SubMaster(["carState", "deviceState", "pandaStates", "selfdriveState", "carControl"])
  deadline = time.monotonic() + 1.0
  required = ("carState", "pandaStates", "selfdriveState", "carControl")
  while time.monotonic() < deadline and not all(sm.seen.get(name, False) for name in required):
    sm.update(100)
  cs = sm["carState"]
  ds = sm["deviceState"]
  ss = sm["selfdriveState"]
  cc = sm["carControl"]
  pandas = sm["pandaStates"]
  try:
    ignition = any(bool(p.ignitionLine or p.ignitionCan) for p in pandas)
  except Exception:
    ignition = False
  try:
    in_park = cs.gearShifter == car.CarState.GearShifter.park
  except Exception:
    in_park = False
  try:
    panda_controls_allowed = any(bool(p.controlsAllowed) for p in pandas)
    door_safety_enabled = any((int(p.safetyParam) & DOOR_SAFETY_FLAG) != 0 for p in pandas)
    safety_params = [int(p.safetyParam) for p in pandas]
  except Exception:
    panda_controls_allowed = True
    door_safety_enabled = False
    safety_params = []
  selfdrive_active = bool(getattr(ss, "active", False))
  lat_active = bool(getattr(cc, "latActive", False))
  long_active = bool(getattr(cc, "longActive", False))
  return {
    "started": bool(getattr(ds, "started", False)),
    "ignition": ignition,
    "park": in_park,
    "v_ego": float(getattr(cs, "vEgo", 999.0)),
    "can_valid": bool(getattr(cs, "canValid", False)),
    "engaged": selfdrive_active or lat_active or long_active,
    "selfdrive_enabled": bool(getattr(ss, "enabled", False)),
    "selfdrive_active": selfdrive_active,
    "lat_active": lat_active,
    "long_active": long_active,
    "panda_controls_allowed": panda_controls_allowed,
    "door_safety_enabled": door_safety_enabled,
    "safety_params": safety_params,
  }


def _door_crc8(payload_without_crc: bytes) -> int:
  """CRC-8 observed on physical 0x3FF frames: poly=0x1D init=0xFF xorout=0xFE."""
  crc = 0xFF
  for byte in payload_without_crc:
    crc ^= byte
    for _ in range(8):
      crc = (((crc << 1) ^ 0x1D) if (crc & 0x80) else (crc << 1)) & 0xFF
  return crc ^ 0xFE


def _door_payload(action: str, counter: int, unlock_state: int = 0x15) -> bytes:
  counter &= 0xF
  if action == "lock":
    body = bytes([(counter << 4) | 0x0, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00])
  elif action == "unlock":
    if unlock_state not in (0x05, 0x15):
      raise ValueError("invalid unlock state")
    body = bytes([(counter << 4) | 0x4, 0x01, unlock_state, 0x00, 0x00, 0x00, 0x00])
  else:
    raise ValueError("action must be lock or unlock")
  return bytes([_door_crc8(body)]) + body


def _read_counter() -> int:
  try:
    raw = json.loads(DOOR_COUNTER_PATH.read_text(encoding="utf-8"))
    return int(raw.get("counter", DOOR_COUNTER_DEFAULT)) & 0xF
  except Exception:
    return DOOR_COUNTER_DEFAULT


def _write_counter(counter: int) -> None:
  try:
    DOOR_COUNTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DOOR_COUNTER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"counter": int(counter) & 0xF, "ts": time.time()}), encoding="utf-8")
    tmp.replace(DOOR_COUNTER_PATH)
  except Exception:
    pass


def _next_counter(explicit: Any = None) -> int:
  if explicit is not None:
    value = int(explicit, 0) if isinstance(explicit, str) else int(explicit)
    if not 0 <= value <= 15:
      raise ValueError("counter must be 0..15")
    return value
  return (_read_counter() + 1) & 0xF


def _build_frames(action: str, counter: int) -> list[tuple[int, bytes, int, int]]:
  # Physical captures repeat the same event payload about three times.
  payload = _door_payload(action, counter)
  return [
    (DOOR_ADDR, payload, DOOR_BUS, 0),
    (DOOR_ADDR, payload, DOOR_BUS, 30),
    (DOOR_ADDR, payload, DOOR_BUS, 30),
  ]


def _get_send_sock():
  global _DOOR_SEND_SOCK
  if _DOOR_SEND_SOCK is None:
    _DOOR_SEND_SOCK = messaging.pub_sock("koDoorCanV1")  # KO_DOOR_IPC_V2
  return _DOOR_SEND_SOCK


def _collect_tx_returns(sock, payloads: set[bytes], timeout_s: float = 0.35) -> dict[str, Any]:
  end = time.monotonic() + timeout_s
  returned = 0
  rejected = 0
  raw: list[dict[str, Any]] = []
  while time.monotonic() < end:
    msg = messaging.recv_one(sock)
    if msg is None:
      continue
    for c in msg.can:
      if int(c.address) != DOOR_ADDR:
        continue
      dat = bytes(c.dat)
      if dat not in payloads:
        continue
      src = int(c.src)
      raw.append({"src": src, "data": dat.hex()})
      if src == 128 + DOOR_BUS:
        returned += 1
      elif src == 192 + DOOR_BUS:
        rejected += 1
  status = "rejected" if rejected else ("returned" if returned else "unknown")  # KO_DOOR_IPC_V2
  return {"status": status, "returned": returned, "rejected": rejected, "raw": raw}


def _door_log(payload: dict[str, Any]) -> None:
  try:
    DOOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DOOR_LOG_PATH.open("a", encoding="utf-8") as f:
      f.write(json.dumps({"ts": time.time(), **payload}, ensure_ascii=False, separators=(",", ":")) + "\n")
  except Exception:
    pass


async def status(request: web.Request) -> web.Response:
  params = Params()
  return web.json_response({
    "ok": True,
    "version": "KOPilot v1.9",
    "pet_mode": params.get_bool("KoPetMode"),
    "door_control_enabled": params.get_bool("KoDoorControlEnabled"),
    "door_protocol": DOOR_PROTOCOL,
    "door_address": hex(DOOR_ADDR),
    "door_bus": DOOR_BUS,
    "door_state_endpoint": "/api/ko/door/state",
    "door_actuation_available": False,
    "door_counter_last": _read_counter(),
    "live": _live_state(),
  })


async def pet_mode(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    body = {}
  enabled = bool(body.get("enabled", False))
  params = Params()
  params.put_bool("KoPetMode", enabled)
  if enabled:
    params.put_bool("CarrotVisionEnabled", True)
  return web.json_response({"ok": True, "enabled": enabled})




def _read_ko_door_lock_state() -> dict:
  # KO_DOOR_STATUS_V2: READ ONLY. No vehicle CAN transmit or safety changes.
  samples = []
  sock = messaging.sub_sock("can", conflate=False, timeout=100)
  start = time.monotonic()
  while time.monotonic() - start < 1.2:
    msg = messaging.recv_one_or_none(sock)
    now = time.monotonic()
    if msg is None:
      time.sleep(0.005)
      continue
    for frame in msg.can:
      if int(frame.src) == 0 and int(frame.address) in (0x411, 0x414):
        samples.append((now, int(frame.src), int(frame.address), bytes(frame.dat)))
  return infer_lock_state(samples, time.monotonic())


async def ko_door_state(request: web.Request) -> web.Response:
  try:
    state = await asyncio.to_thread(_read_ko_door_lock_state)
  except Exception as exc:
    return web.json_response({"ok": False, "state": "unknown", "locked": None,
                              "error": f"door_status_read_failed: {exc}"}, status=503)
  return web.json_response({"ok": True, "door_state": state,
                            "door_actuation_available": False, "can_transmitted": False})

async def door_command(request: web.Request) -> web.Response:
  # KO_DOOR_STATUS_V2: block unverified 0x3FF actuation; retain original TX code for later validation.
  action = request.match_info.get("action", "").strip().lower()
  if action not in ("lock", "unlock"):
    return web.json_response({"ok": False, "error": "action must be lock or unlock"}, status=400)
  try:
    lock_status = await asyncio.to_thread(_read_ko_door_lock_state)
  except Exception:
    lock_status = {"state": "unknown", "locked": None, "corroborated": False}
  return web.json_response({"ok": False, "action": action,
                            "error": "door_actuation_protocol_unverified",
                            "transmitted": False, "door_actuation_available": False,
                            "door_state": lock_status}, status=409)
  action = request.match_info.get("action", "").strip().lower()
  if action not in ("lock", "unlock"):
    return web.json_response({"ok": False, "error": "action must be lock or unlock"}, status=400)

  try:
    body = await request.json()
  except Exception:
    body = {}

  params = Params()
  if not params.get_bool("KoDoorControlEnabled"):
    return web.json_response({"ok": False, "error": "KO door control is disabled"}, status=403)

  state = _live_state()
  blocked = []
  if not state["ignition"]:
    blocked.append("ignition_on_required")
  if not state["park"]:
    blocked.append("park_required")
  if abs(state["v_ego"]) > 0.1:
    blocked.append("standstill_required")
  if not state["can_valid"]:
    blocked.append("can_valid_required")
  if state["engaged"]:
    blocked.append("disengage_required")
  if state["panda_controls_allowed"]:
    blocked.append("panda_controls_allowed_must_be_false")
  if not state["door_safety_enabled"]:
    blocked.append("door_safety_flag_not_active_reboot_required")
  if blocked:
    return web.json_response({"ok": False, "blocked": blocked, "live": state}, status=409)

  async with _DOOR_COMMAND_LOCK:
    try:
      counter = _next_counter(body.get("counter"))
      frames = _build_frames(action, counter)
    except Exception as exc:
      return web.json_response({"ok": False, "error": str(exc)}, status=400)

    rx = messaging.sub_sock("can", conflate=False, timeout=50)
    payloads = {data for _, data, _, _ in frames}
    try:
      sock = _get_send_sock()
      for address, data, bus, delay_ms in frames:
        if delay_ms > 0:
          await asyncio.sleep(delay_ms / 1000.0)
        sock.send(can_list_to_can_capnp([(address, data, bus)], msgtype="sendcan", valid=True))
      tx_result = await asyncio.to_thread(_collect_tx_returns, rx, payloads)
    except Exception as exc:
      _door_log({"ok": False, "action": action, "counter": counter, "error": str(exc), "live": state})
      return web.json_response({"ok": False, "error": f"sendcan failed: {exc}"}, status=500)

    # Advance only after the request was emitted. The explicit counter path is
    # also persisted so subsequent Web commands continue from the same value.
    _write_counter(counter)
    payload_hex = [data.hex() for _, data, _, _ in frames]
    result = {
      "ok": tx_result["status"] == "returned",
      "action": action,
      "counter": counter,
      "frames": len(frames),
      "payloads": payload_hex,
      "panda_tx": tx_result,
      "live": state,
    }
    _door_log(result)
    status_code = 200 if result["ok"] else 409
    return web.json_response(result, status=status_code)


def register(app: web.Application) -> None:
  app.router.add_get("/api/ko/vehicle/status", status)
  app.router.add_post("/api/ko/pet_mode", pet_mode)
  app.router.add_get("/api/ko/door/state", ko_door_state)
  app.router.add_post("/api/ko/door/{action}", door_command)
