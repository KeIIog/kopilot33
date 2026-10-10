"""KOPilot Hyundai door command experiment CAN evidence recorder (read only).

Borrowed architecture (not vehicle CAN payloads): sunnypilot Toyota 0x750 controller
and safety checks. Hyundai egmp diagnostics as documented by evDash are NOT
validated door actuator requests. Only v2.4's original 0x3FF attempt is used.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from aiohttp import web
from openpilot.cereal import messaging

PROBE_DIR = Path('/data/ko/door_probe')
SUMMARY = Path('/data/ko/door_probe_latest.json')
MAX_PROBE_BYTES = 6 * 1024 * 1024
MAX_FRAMES_PER_TEST = 16000
MAX_SECONDS = 8.0

SOURCES = {
  'ioniq5_3ff_existing_trial': {
    'mode': 'existing_safety_gated_transmit_only',
    'actuator_protocol_verified': False,
    'notes': 'Known existing experiment, not a proven lock/unlock actuator command',
  },
  'sunnypilot_toyota_0x750': {
    'mode': 'REFERENCE_ONLY_NEVER_TRANSMIT_ON_HYUNDAI',
    'source': 'https://github.com/sunnypilot/opendbc/blob/toyota-auto-lock-unlock/opendbc/sunnypilot/car/toyota/auto_lock_unlock.py',
    'notes': 'Use only for controller structure, Safety, park / speed gates',
  },
  'hyundai_egmp_bcm_0x7a0': {
    'mode': 'REFERENCE_ONLY_DIAGNOSTIC_ID_NOT_LOCK_COMMAND',
    'source': 'https://github.com/nickn17/evDash/blob/master/src/CarHyundaiEgmp.cpp',
    'notes': 'BCM/TPM UDS address is not a validated lock actuator command',
  },
}


def _collect(stop: threading.Event) -> dict:
  begin = time.monotonic()
  start_ns = time.time_ns()
  frames: list[tuple] = []
  counts: Counter[str] = Counter()
  try:
    sock = messaging.sub_sock('can', conflate=False, timeout=100)
    while not stop.is_set() and time.monotonic() - begin < MAX_SECONDS:
      item = messaging.recv_one_or_none(sock)
      now = round(time.monotonic() - begin, 5)
      if item is None:
        time.sleep(0.003)
        continue
      for c in item.can:
        src = int(c.src)
        if src not in (0, 1, 2, 128, 129, 130, 192, 193, 194):
          continue
        addr = int(c.address)
        counts[f'{src}:{addr:X}'] += 1
        if len(frames) < MAX_FRAMES_PER_TEST:
          frames.append((now, src, addr, bytes(c.dat).hex()))
    return {'start_ns': start_ns, 'elapsed_s': round(time.monotonic() - begin, 3),
            'frames': frames, 'total_seen': sum(counts.values()),
            'truncated': sum(counts.values()) > len(frames),
            'counts': counts}
  except Exception as exc:
    return {'start_ns': start_ns, 'elapsed_s': round(time.monotonic() - begin, 3),
            'frames': frames, 'total_seen': sum(counts.values()), 'counts': counts,
            'capture_error': repr(exc)}


def _atomically_write_json(path: Path, data: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_suffix('.json.tmp')
  temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str), encoding='utf-8')
  temporary.replace(path)


def _store(action: str, collected: dict, response: object | None) -> dict:
  PROBE_DIR.mkdir(parents=True, exist_ok=True)
  stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
  path = PROBE_DIR / f'door_probe_{stamp}_{action}.jsonl.gz'
  raw = getattr(response, 'text', '') if response is not None else ''
  try:
    outcome = json.loads(raw) if raw else None
  except Exception:
    outcome = {'raw_response': str(raw)[:600]}
  if not isinstance(outcome, dict):
    outcome = {'response': outcome}
  summary = {
    'ok': True, 'capture_only': True, 'action': action,
    'recorded_at': datetime.now().isoformat(timespec='seconds'),
    'file': path.name, 'frames_written': len(collected.get('frames', [])),
    'frames_seen': collected.get('total_seen', 0),
    'truncated': collected.get('truncated', False),
    'capture_error': collected.get('capture_error'),
    'duration_s': collected.get('elapsed_s'),
    'event_ids': {str(k): sum(1 for r in collected.get('frames', []) if r[2] == k)
                  for k in (0x3FF, 0x411, 0x414, 0x7A0, 0x7A8)},
    'attempt': {'http_status': getattr(response, 'status', None),
                'transmitted': outcome.get('transmitted'),
                'panda_tx': outcome.get('panda_tx'),
                'actuator_state_transition_confirmed': outcome.get('actuator_state_transition_confirmed'),
                'door_state_before': outcome.get('door_state_before'),
                'door_state_after': outcome.get('door_state_after'),
                'error': outcome.get('error')},
    'known_protocol': 'UNVERIFIED_0x3FF',
  }
  with gzip.open(path, 'wt', encoding='utf-8', compresslevel=5) as output:
    output.write(json.dumps({'type':'metadata', **summary}, ensure_ascii=False, default=str)+'\n')
    for elapsed, bus, addr, payload in collected.get('frames', []):
      output.write(json.dumps({'t':elapsed,'src':bus,'id':f'{addr:03X}','data':payload},separators=(',', ':'))+'\n')
  _atomically_write_json(SUMMARY, summary)
  # Keep the newest probe; older evidence is deleted only when full, separately from driving logs.
  logs = sorted(PROBE_DIR.glob('door_probe_*.jsonl.gz'), key=lambda p: (p.stat().st_mtime_ns, p.name))
  used = sum(p.stat().st_size for p in logs)
  for old in logs:
    if used <= MAX_PROBE_BYTES:
      break
    if old == path:
      continue
    size = old.stat().st_size
    old.unlink()
    used -= size
  return summary


async def record_one_experiment(action: str, original_handler):
  """Wrap v2.4's opt-in Panda-safety-gated TX with independent RX evidence."""
  stop = threading.Event()
  collector = asyncio.create_task(asyncio.to_thread(_collect, stop))
  await asyncio.sleep(0.05)  # give the subscriber time to register
  response = None
  try:
    response = await original_handler(action)
    return response
  finally:
    stop.set()
    try:
      observations = await asyncio.wait_for(collector, timeout=2.0)
      await asyncio.to_thread(_store, action, observations, response)
    except Exception as exc:
      # A recorder failure is visible in status, but must not trigger retry TX.
      _atomically_write_json(SUMMARY, {'ok':False, 'error':'capture_failed: '+str(exc),
                                       'action':action, 'timestamp':time.time()})


async def get_probe_status(request: web.Request) -> web.Response:
  try:
    state = json.loads(SUMMARY.read_text('utf-8'))
  except (OSError, ValueError):
    state = {'ok':False,'error':'no probe yet'}
  return web.json_response({'ok':True,'latest':state,'candidates':SOURCES,
                            'note':'Only 0x3FF existing protected experiment is transmitted; no Toyota or BCM UDS commands sent'})


async def download_probe(request: web.Request) -> web.StreamResponse:
  try:
    state = json.loads(SUMMARY.read_text('utf-8'))
    name = state.get('file')
    if not isinstance(name, str) or not name.startswith('door_probe_') or '/' in name:
      raise ValueError('invalid filename')
    path = PROBE_DIR / name
    if not path.is_file():
      raise FileNotFoundError(path)
  except (OSError, ValueError, TypeError):
    return web.json_response({'ok':False,'error':'no successful probe file'},status=404)
  return web.FileResponse(path,headers={'Content-Disposition':f'attachment; filename="{path.name}"',
                                       'Cache-Control':'no-store'})
