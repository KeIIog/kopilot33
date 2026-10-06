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

DOOR_CONFIG_PATH = Path(os.environ.get("KO_DOOR_CAN_CONFIG", "/data/ko/door_can.json"))
DOOR_LOG_PATH = Path(os.environ.get("KO_DOOR_CONTROL_LOG", "/data/ko/door_control.jsonl"))


def _live_state() -> dict[str, Any]:
  sm = messaging.SubMaster(["carState", "deviceState", "pandaStates", "selfdriveState"])
  sm.update(250)
  cs = sm["carState"]
  ds = sm["deviceState"]
  ss = sm["selfdriveState"]
  pandas = sm["pandaStates"]
  try:
    ignition = any(bool(p.ignitionLine or p.ignitionCan) for p in pandas)
  except Exception:
    ignition = False
  try:
    in_park = cs.gearShifter == car.CarState.GearShifter.park
  except Exception:
    in_park = False
  return {
    "started": bool(getattr(ds, "started", False)),
    "ignition": ignition,
    "park": in_park,
    "v_ego": float(getattr(cs, "vEgo", 999.0)),
    "can_valid": bool(getattr(cs, "canValid", False)),
    "engaged": bool(getattr(ss, "enabled", False)),
  }


def _load_frames(action: str) -> list[tuple[int, bytes, int, int]]:
  if not DOOR_CONFIG_PATH.is_file():
    raise FileNotFoundError(str(DOOR_CONFIG_PATH))
  raw = json.loads(DOOR_CONFIG_PATH.read_text(encoding="utf-8"))
  items = raw.get(action)
  if not isinstance(items, list) or not items:
    raise ValueError(f"no verified '{action}' frames")
  out: list[tuple[int, bytes, int, int]] = []
  for i, frame in enumerate(items):
    if not isinstance(frame, dict):
      raise ValueError(f"frame {i} is not an object")
    a = frame.get("address")
    address = int(a, 0) if isinstance(a, str) else int(a)
    bus = int(frame.get("bus"))
    data = bytes.fromhex(str(frame.get("data", "")).replace(" ", ""))
    delay_ms = int(frame.get("delay_ms", 0) or 0)
    if not (0 <= address <= 0x1FFFFFFF):
      raise ValueError(f"frame {i} address out of range")
    if not (0 <= bus <= 3):
      raise ValueError(f"frame {i} bus out of range")
    if not (1 <= len(data) <= 64):
      raise ValueError(f"frame {i} payload length invalid")
    if not (0 <= delay_ms <= 1000):
      raise ValueError(f"frame {i} delay_ms out of range")
    out.append((address, data, bus, delay_ms))
  return out


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
    "pet_mode": params.get_bool("KoPetMode"),
    "door_control_enabled": params.get_bool("KoDoorControlEnabled"),
    "door_config_present": DOOR_CONFIG_PATH.is_file(),
    "door_config_path": str(DOOR_CONFIG_PATH),
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


async def door_command(request: web.Request) -> web.Response:
  action = request.match_info.get("action", "").strip().lower()
  if action not in ("lock", "unlock"):
    return web.json_response({"ok": False, "error": "action must be lock or unlock"}, status=400)
  params = Params()
  if not params.get_bool("KoDoorControlEnabled"):
    return web.json_response({"ok": False, "error": "KO door control is disabled"}, status=403)
  state = _live_state()
  blocked = []
  if not state["started"] or not state["ignition"]:
    blocked.append("ignition_on_required")
  if not state["park"]:
    blocked.append("park_required")
  if abs(state["v_ego"]) > 0.1:
    blocked.append("standstill_required")
  if not state["can_valid"]:
    blocked.append("can_valid_required")
  if state["engaged"]:
    blocked.append("disengage_required")
  if blocked:
    return web.json_response({"ok": False, "blocked": blocked, "live": state}, status=409)
  try:
    frames = _load_frames(action)
  except FileNotFoundError:
    return web.json_response({
      "ok": False,
      "error": "verified door CAN config missing; no built-in IONIQ 5 lock/unlock frame is provided",
      "config_path": str(DOOR_CONFIG_PATH),
    }, status=409)
  except Exception as exc:
    return web.json_response({"ok": False, "error": f"invalid door CAN config: {exc}"}, status=400)
  try:
    sock = messaging.pub_sock("sendcan")
    for address, data, bus, delay_ms in frames:
      if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)
      sock.send(can_list_to_can_capnp([(address, data, bus)], msgtype="sendcan", valid=True))
  except Exception as exc:
    _door_log({"ok": False, "action": action, "error": str(exc), "live": state})
    return web.json_response({"ok": False, "error": f"sendcan failed: {exc}"}, status=500)
  _door_log({"ok": True, "action": action, "frames": len(frames), "live": state})
  return web.json_response({"ok": True, "action": action, "frames": len(frames), "live": state})


def register(app: web.Application) -> None:
  app.router.add_get("/api/ko/vehicle/status", status)
  app.router.add_post("/api/ko/pet_mode", pet_mode)
  app.router.add_post("/api/ko/door/{action}", door_command)
