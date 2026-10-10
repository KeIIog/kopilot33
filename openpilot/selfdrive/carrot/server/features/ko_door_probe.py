"""Hyundai Ioniq 5 passive door command candidate evidence recorder (v2.5.2).

Vehicle CAN TX, UDS diagnostics, Panda Safety changes are NEVER made here.
The user physically actuates a door button/fob during a 12-second recording.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from aiohttp import web
from openpilot.cereal import messaging
from .ko_door_state import decode_lock_indicator

PROBE_DIR = Path('/data/ko/door_probe')
SUMMARY = Path('/data/ko/door_probe_latest.json')
DURATION_S = 12.0
MAX_CAPTURE_BYTES = 16_000_000  # compressed bytes; safeguards storage, not a frame count
MAX_TOTAL_PROBE_BYTES = 40_000_000  # separate from drive log 100MB limit
CANDIDATES = {
  '0x3FF': 'prior unsuccessful transmission; observe only, likely event/status',
  '0x411': 'local read-only door/lock indicator (unverified OEM mapping)',
  '0x414': 'local read-only corroborating indicator (unverified OEM mapping)',
  '0x4A2': 'can-do E-GMP community door-control candidate; unverified on this vehicle',
  '0x587': 'can-do E-GMP community telematics candidate; unverified on this vehicle',
  '0x588': 'community telematics body candidate; read only',
  '0x770': 'older-Ioniq IGPM diagnostic address; may differ on Ioniq 5',
  '0x7A0': 'older-Ioniq BCM diagnostic address; NOT a known actuator command',
}
TRACKED = frozenset(int(k, 16) for k in CANDIDATES)
CAPTURE_LOCK = asyncio.Lock()


def _safe_write(path: Path, value: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_suffix('.tmp')
  tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str), encoding='utf-8')
  tmp.replace(path)


def _state_from_buffers(first: dict, last: dict) -> dict:
  def stage(buffers):
    result = []
    for key in (0x411, 0x414):
      b = buffers.get(key, [])
      if len(b) < 2 or b[-1] is None or b[-2] is None or b[-2] != b[-1]:
        return 'unknown'
      result.append(b[-1])
    return ('locked' if result[0] else 'unlocked') if result[0] == result[1] else 'unknown'
  return {'before':stage(first), 'after':stage(last),
          'note':'inferred 0x411/0x414 status only; not an OEM actuator acknowledgement'}


def _stream_capture(action: str, duration_s: float = DURATION_S, sock=None, clock=None, receiver=None):
  """Stream every RX frame directly to gzip; no in-memory frame-list/frame-count cap.

  Optional clock/receiver/sock are only for offline tests, not HTTP-controlled.
  """
  PROBE_DIR.mkdir(parents=True, exist_ok=True)
  stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
  path = PROBE_DIR / ('door_probe_' + stamp + '_' + action + '.jsonl.gz')
  clock = clock or time.monotonic
  sock = sock if sock is not None else messaging.sub_sock('can', conflate=False, timeout=75)
  receiver = receiver or messaging.recv_one_or_none
  start = clock()
  frames_seen = 0
  frames_written = 0
  tx_returns = 0
  by_bus_id: Counter[str] = Counter()
  seen_candidate: Counter[str] = Counter()
  payload_changes: Counter[str] = Counter()
  first_candidate_payloads = {}
  last_candidate_payloads = {}
  prior_payloads = {}
  state_first = defaultdict(list)
  state_last = defaultdict(list)
  last_state = {}
  state_transition_events = []
  truncated = False
  capture_error = None
  written_since_check = 0
  try:
    with gzip.open(path, 'wt', encoding='utf-8', compresslevel=3) as gz:
      gz.write(json.dumps({'type':'metadata','version':'KOPilot v2.5.2',
                           'action_expected':action,'capture_only':True,
                           'can_transmitted':False,'duration_target_s':duration_s,
                           'candidate_descriptions':CANDIDATES},ensure_ascii=False)+'\n')
      while clock() - start < duration_s:
        item = receiver(sock)
        if item is None:
          time.sleep(0.002)
          continue
        elapsed = round(clock() - start, 5)
        for frame in item.can:
          src = int(frame.src)
          addr = int(frame.address)
          frames_seen += 1
          # 128+ are Panda TX echo, not actual vehicle RX. Keep only candidate TX echoes.
          if src in (128,129,130,192,193,194):
            if addr in TRACKED:
              tx_returns += 1
              gz.write(json.dumps({'t':elapsed,'src':src,'id':f'{addr:03X}',
                                   'data':bytes(frame.dat).hex(),'kind':'panda_tx_echo'},separators=(',',':'))+'\n')
              frames_written += 1
            continue
          if src not in (0,1,2):
            continue
          data = bytes(frame.dat)
          key = f'{src}:{addr:03X}'
          by_bus_id[key] += 1
          # No frame count limit; raw physical bus frames are streamed to disk.
          gz.write(json.dumps({'t':elapsed,'src':src,'id':f'{addr:03X}',
                               'data':data.hex()},separators=(',',':'))+'\n')
          frames_written += 1
          if addr in TRACKED:
            seen_candidate[key] += 1
            if key not in first_candidate_payloads:
              first_candidate_payloads[key] = data.hex()
            last_candidate_payloads[key] = data.hex()
            if key in prior_payloads and prior_payloads[key] != data:
              payload_changes[key] += 1
            prior_payloads[key] = data
          if src == 0 and addr in (0x411,0x414):
            value = decode_lock_indicator(addr,data)
            if len(state_first[addr]) < 2:
              state_first[addr].append(value)
            state_last[addr].append(value)
            if len(state_last[addr])>2:
              state_last[addr].pop(0)
            if value is not None and addr in last_state and last_state[addr] != value:
              if len(state_transition_events) < 50:
                state_transition_events.append({'t':elapsed,'can_id':f'0x{addr:X}',
                                                'lock_inferred':bool(value)})
            if value is not None:
              last_state[addr] = value
        written_since_check += 1
        if written_since_check >= 64:
          gz.flush()
          written_since_check = 0
          if path.stat().st_size > MAX_CAPTURE_BYTES:
            truncated = True
            capture_error = 'compressed_file_size_safety_limit'
            break
  except Exception as exc:
    capture_error = 'capture_exception: '+str(exc)
  elapsed = round(clock()-start, 3)
  states = _state_from_buffers(state_first,state_last)
  summary = {
    'ok':capture_error is None and frames_written>0,
    'capture_only':True,'can_transmitted':False,
    'action_expected':action,'file':path.name,'recorded_at':datetime.now().isoformat(timespec='seconds'),
    'duration_s':elapsed,'frames_seen':frames_seen,'frames_written':frames_written,
    'truncated':truncated,'capture_error':capture_error,
    'candidate_counts':{k:int(v) for k,v in seen_candidate.items()},
    'candidate_payload_changes':{k:int(v) for k,v in payload_changes.items()},
    'candidate_first_payloads':first_candidate_payloads,
    'candidate_last_payloads':last_candidate_payloads,
    'top_bus_ids':dict(by_bus_id.most_common(20)),
    'panda_tx_echoes_for_candidates':tx_returns,
    'inferred_door_state':states,'inferred_transition_events':state_transition_events,
    'notes': ['Physical-button/fob action expected during capture. No vehicle commands generated.',
              '0x4A2/0x587 are community candidates, presence does NOT establish actuator control.',
              '0x770/0x7A0 are observed passively; diagnostic services are NEVER sent.',
              'No bus data may indicate comma hardware is not connected to powered vehicle.'],
  }
  if frames_written==0 and capture_error is None:
    summary['ok']=False
    summary['capture_error']='no_vehicle_CAN_frames_captured'
  _safe_write(SUMMARY,summary)
  # Rotate only completed evidence files. The latest is NEVER removed.
  try:
    logs=sorted(PROBE_DIR.glob('door_probe_*.jsonl.gz'),key=lambda p:(p.stat().st_mtime_ns,p.name))
    total=sum(p.stat().st_size for p in logs)
    for p in logs:
      if total<=MAX_TOTAL_PROBE_BYTES:break
      if p==path:continue
      n=p.stat().st_size
      p.unlink()
      total-=n
  except OSError:
    pass
  return summary


async def physical_capture(request: web.Request) -> web.Response:
  try:
    body=await request.json()
  except Exception:
    body={}
  if not isinstance(body,dict) or body.get('confirmation')!='PHYSICAL_BUTTON_CAPTURE_NO_TX':
    return web.json_response({'ok':False,'error':'explicit_physical_capture_confirmation_required'},status=400)
  action=body.get('action')
  if action not in ('lock','unlock'):
    return web.json_response({'ok':False,'error':'action must be lock or unlock'},status=400)
  if CAPTURE_LOCK.locked():
    return web.json_response({'ok':False,'error':'capture_already_running'},status=409)
  async with CAPTURE_LOCK:
    try:
      result=await asyncio.to_thread(_stream_capture,action)
    except Exception as exc:
      return web.json_response({'ok':False,'capture_only':True,'can_transmitted':False,
                                'error':'CAN_subscription_failed: '+str(exc)},status=503)
  return web.json_response(result,status=200 if result['ok'] else 409)


async def get_probe_status(request: web.Request) -> web.Response:
  try:
    summary=json.loads(SUMMARY.read_text(encoding='utf-8'))
  except (OSError,ValueError):
    summary={'ok':False,'error':'no passive capture yet'}
  return web.json_response({'ok':True,'version':'2.5.2','latest':summary,
                            'candidate_descriptions':CANDIDATES,
                            'transmission_enabled':False})


async def download_probe(request: web.Request) -> web.StreamResponse:
  try:
    latest=json.loads(SUMMARY.read_text(encoding='utf-8'))
    # No arbitrary path accepted. Optional name must match an existing probe basename.
    filename=request.query.get('name') or latest.get('file')
    if not isinstance(filename,str) or not filename.startswith('door_probe_') or not filename.endswith('.jsonl.gz') or '/' in filename or '\\' in filename or '..' in filename:
      raise ValueError('bad filename')
    path=PROBE_DIR/filename
    if not path.is_file():raise FileNotFoundError(path)
  except (OSError,ValueError,TypeError):
    return web.json_response({'ok':False,'error':'probe_file_not_found'},status=404)
  return web.FileResponse(path,headers={'Content-Disposition':'attachment; filename="'+path.name+'"',
                                       'Cache-Control':'no-store','Content-Type':'application/gzip'})


async def probe_history(request: web.Request) -> web.Response:
  files=sorted(PROBE_DIR.glob('door_probe_*.jsonl.gz'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
  return web.json_response({'ok':True,'files':[{'name':p.name,'bytes':p.stat().st_size} for p in files[:40]],
                            'total_bytes':sum(p.stat().st_size for p in files),
                            'max_bytes':MAX_TOTAL_PROBE_BYTES})


async def record_one_experiment(action: str, original_handler):
  """Backwards compatible name but never invokes legacy 0x3FF sender."""
  return web.json_response({'ok':False,'error':'old_0x3FF_test_retired_use_physical_capture',
                            'can_transmitted':False},status=409)
