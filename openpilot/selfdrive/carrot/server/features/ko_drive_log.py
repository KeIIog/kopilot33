from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web
from openpilot.cereal import messaging
from openpilot.common.params import Params

LOG_DIR = Path(os.environ.get("KO_DRIVE_LOG_DIR", "/data/ko/drive_logs"))
SAMPLE_HZ = 10.0
SERVICES = [
  "carState",
  "carControl",
  "carOutput",
  "controlsState",
  "selfdriveState",
  "radarState",
  "liveTracks",
  "longitudinalPlan",
  "liveTorqueParameters",
  "liveParameters",
  "modelV2",
]

_PARAM_KEYS_INT = (
  "LateralTorqueCustom",
  "LateralTorqueAccelFactor",
  "LateralTorqueFriction",
  "LateralTorqueKpV",
  "LateralTorqueKiV",
  "LateralTorqueKd",
  "LateralTorqueKf",
  "EnableRadarTracks",
  "EnableCornerRadar",
  "RadarTrackFlip",
)

_state_lock = threading.Lock()
_file_lock = threading.Lock()
_stop_event = threading.Event()
_worker: threading.Thread | None = None
_state: dict[str, Any] = {
  "enabled": False,
  "path": None,
  "started_at": 0.0,
  "samples": 0,
  "last_error": "",
}


def _jsonable(value: Any) -> Any:
  try:
    return value.to_dict()
  except Exception:
    return str(value)


def _params_snapshot() -> dict[str, Any]:
  params = Params()
  out: dict[str, Any] = {}
  for key in _PARAM_KEYS_INT:
    try:
      out[key] = params.get_int(key)
    except Exception:
      out[key] = None
  return out


def _model_snapshot(model: Any) -> dict[str, Any]:
  leads = []
  try:
    for lead in list(model.leadsV3)[:2]:
      leads.append(_jsonable(lead))
  except Exception:
    pass

  action: dict[str, Any] = {}
  try:
    action = _jsonable(model.action)
  except Exception:
    pass

  meta: dict[str, Any] = {}
  try:
    m = model.meta
    meta = {
      "hardBrakePredicted": bool(m.hardBrakePredicted),
      "laneChangeState": str(m.laneChangeState),
      "laneChangeDirection": str(m.laneChangeDirection),
      "desire": str(m.desire),
      "modelTurnSpeed": float(m.modelTurnSpeed),
    }
  except Exception:
    pass

  return {
    "frameId": int(getattr(model, "frameId", 0)),
    "frameAge": int(getattr(model, "frameAge", 0)),
    "modelExecutionTime": float(getattr(model, "modelExecutionTime", 0.0)),
    "leadsV3": leads,
    "action": action,
    "meta": meta,
  }


def _sample(sm: Any, t0: float) -> dict[str, Any]:
  row: dict[str, Any] = {
    "type": "sample",
    "t": round(time.monotonic() - t0, 4),
    "wall": time.time(),
  }
  for service in SERVICES:
    try:
      if service == "modelV2":
        row[service] = _model_snapshot(sm[service])
      else:
        row[service] = _jsonable(sm[service])
      row[f"{service}_valid"] = bool(sm.valid.get(service, False))
      row[f"{service}_mono"] = int(sm.logMonoTime.get(service, 0))
    except Exception as exc:
      row[service] = {"error": str(exc)}
  return row


def _latest_log() -> Path | None:
  try:
    files = sorted(LOG_DIR.glob("KOPilot_drive_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None
  except Exception:
    return None


def _logger_worker(path: Path, started_mono: float) -> None:
  sm = messaging.SubMaster(SERVICES)
  interval = 1.0 / SAMPLE_HZ
  next_sample = time.monotonic()
  samples = 0
  last_params: dict[str, Any] | None = None

  try:
    with path.open("a", encoding="utf-8", buffering=1) as f:
      while not _stop_event.is_set():
        sm.update(100)
        now = time.monotonic()
        if now < next_sample:
          continue
        next_sample = now + interval

        row = _sample(sm, started_mono)
        if samples % int(SAMPLE_HZ) == 0:
          params = _params_snapshot()
          if params != last_params:
            row["tuningParams"] = params
            last_params = params

        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        with _file_lock:
          f.write(line)
          f.flush()

        samples += 1
        with _state_lock:
          _state["samples"] = samples
  except Exception as exc:
    with _state_lock:
      _state["last_error"] = str(exc)
  finally:
    with _state_lock:
      _state["enabled"] = False


def _start_logging() -> Path:
  global _worker
  LOG_DIR.mkdir(parents=True, exist_ok=True)

  now = datetime.now()
  path = LOG_DIR / f"KOPilot_drive_{now.strftime('%Y%m%d_%H%M%S')}.jsonl"
  started_mono = time.monotonic()
  meta = {
    "type": "meta",
    "schema": "kopilot-drive-log-v1",
    "created_at": now.isoformat(timespec="seconds"),
    "sample_hz": SAMPLE_HZ,
    "services": SERVICES,
    "params": _params_snapshot(),
    "note": "No camera frames or GPS coordinates are included.",
  }
  path.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

  _stop_event.clear()
  with _state_lock:
    _state.update({
      "enabled": True,
      "path": str(path),
      "started_at": time.time(),
      "samples": 0,
      "last_error": "",
    })

  _worker = threading.Thread(
    target=_logger_worker,
    args=(path, started_mono),
    daemon=True,
    name="ko-drive-log",
  )
  _worker.start()
  return path


def _stop_logging() -> None:
  global _worker
  _stop_event.set()
  worker = _worker
  if worker is not None and worker.is_alive():
    worker.join(timeout=1.5)
  _worker = None
  with _state_lock:
    _state["enabled"] = False


def _status_payload() -> dict[str, Any]:
  with _state_lock:
    state = dict(_state)

  path = Path(state["path"]) if state.get("path") else _latest_log()
  size = 0
  filename = ""
  if path is not None and path.is_file():
    try:
      size = path.stat().st_size
      filename = path.name
    except Exception:
      pass

  elapsed = (
    max(0.0, time.time() - float(state.get("started_at") or 0.0))
    if state.get("enabled")
    else 0.0
  )
  return {
    "ok": True,
    "enabled": bool(state.get("enabled")),
    "filename": filename,
    "bytes": size,
    "samples": int(state.get("samples") or 0),
    "elapsed_s": elapsed,
    "last_error": str(state.get("last_error") or ""),
  }


def _gzip_snapshot(path: Path) -> Path:
  export_dir = LOG_DIR / "export"
  export_dir.mkdir(parents=True, exist_ok=True)
  target = export_dir / f"{path.name}.gz"
  tmp = export_dir / f".{path.name}.tmp.gz"

  with _file_lock:
    with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
      shutil.copyfileobj(src, dst)

  tmp.replace(target)
  return target


async def status(request: web.Request) -> web.Response:
  return web.json_response(_status_payload())


async def enable(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    body = {}

  enabled = bool(body.get("enabled", False))
  with _state_lock:
    active = bool(_state.get("enabled"))

  if enabled and not active:
    await asyncio.to_thread(_start_logging)
  elif not enabled and active:
    await asyncio.to_thread(_stop_logging)

  return web.json_response(_status_payload())


async def mark(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    body = {}

  with _state_lock:
    path_value = _state.get("path")
    active = bool(_state.get("enabled"))

  if not active or not path_value:
    return web.json_response({"ok": False, "error": "drive log is not recording"}, status=409)

  path = Path(str(path_value))
  label = str(body.get("label", "user_event"))[:80]
  row = {"type": "marker", "wall": time.time(), "label": label}

  try:
    with _file_lock:
      with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
  except Exception as exc:
    return web.json_response({"ok": False, "error": str(exc)}, status=500)

  return web.json_response({"ok": True, "label": label})


async def download(request: web.Request) -> web.StreamResponse:
  with _state_lock:
    path_value = _state.get("path")

  path = Path(str(path_value)) if path_value else _latest_log()
  if path is None or not path.is_file():
    return web.json_response({"ok": False, "error": "no drive log available"}, status=404)

  try:
    gz_path = await asyncio.to_thread(_gzip_snapshot, path)
  except Exception as exc:
    return web.json_response({"ok": False, "error": f"log export failed: {exc}"}, status=500)

  response = web.FileResponse(gz_path)
  response.headers["Content-Disposition"] = f'attachment; filename="{gz_path.name}"'
  response.headers["Cache-Control"] = "no-store"
  return response


def register(app: web.Application) -> None:
  app.router.add_get("/api/ko/drive_log/status", status)
  app.router.add_post("/api/ko/drive_log/enable", enable)
  app.router.add_post("/api/ko/drive_log/mark", mark)
  app.router.add_get("/api/ko/drive_log/download", download)
