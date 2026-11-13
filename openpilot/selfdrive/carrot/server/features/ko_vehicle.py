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
from .ko_egmp_door_candidate import candidate_preview

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
    "egmp_candidate": {"mode": "OFFLINE_ONLY", "address": "0x4A2", "comma_bus": None,
                       "can_transmitted": False, "vehicle_verified": False},
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

# KO_DOOR_EXPERIMENT_V24: explicit, supervised, stationary test of existing 0x3FF hypothesis.
# This is NOT a validated Hyundai lock/unlock protocol. Panda Safety is unchanged.
_DOOR_EXPERIMENT_LAST_MONO = 0.0
_DOOR_EXPERIMENT_LOG = Path('/data/ko/door_experiments_v24.jsonl')


def _door_test_audit(report: dict) -> None:
  _DOOR_EXPERIMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
  with _DOOR_EXPERIMENT_LOG.open('a', encoding='utf-8') as f:
    f.write(json.dumps({'ts': time.time(), **report}, ensure_ascii=False, default=str) + '\n')


async def _run_door_experiment_v24(action: str) -> web.Response:
  global _DOOR_EXPERIMENT_LAST_MONO
  params = Params()
  live = await asyncio.to_thread(_live_state)
  blocked=[]
  if not params.get_bool('KoDoorControlEnabled'):
    blocked.append('KoDoorControlEnabled_off')
  if not live.get('ignition'): blocked.append('ignition_required')
  if not live.get('park'): blocked.append('P_gear_required')
  if abs(live.get('v_ego',100))>0.05: blocked.append('vehicle_must_be_stopped')
  if not live.get('can_valid'): blocked.append('fresh_valid_CAN_required')
  if live.get('engaged') or live.get('panda_controls_allowed'): blocked.append('driving_assist_must_be_off')
  if not live.get('door_safety_enabled'): blocked.append('existing_panda_door_safety_not_enabled')
  if time.monotonic()-_DOOR_EXPERIMENT_LAST_MONO < 20: blocked.append('20_second_cooldown')
  if blocked:
    return web.json_response({'ok':False,'error':'door_experiment_blocked','blocked':blocked,'live':live,
                              'transmitted':False},status=409)
  try:
    before=await asyncio.to_thread(_read_ko_door_lock_state)
  except Exception as exc:
    before={'state':'unknown','error':str(exc)}
  # KO_GAP_TUNE_STATE_REPORT_20261031: verify initial door state.
  if before.get('corroborated') is not True:
    return web.json_response({'ok':False,'error':'initial_door_state_unconfirmed',
                              'door_state_before':before,'can_transmitted':False},status=409)
  wanted=(action=='lock')
  if before.get('corroborated') and before.get('locked')==wanted:
    return web.json_response({'ok':False,'error':'door_already_in_requested_state',
                              'door_state_before':before,'transmitted':False},status=409)
  async with _DOOR_COMMAND_LOCK:
    if time.monotonic()-_DOOR_EXPERIMENT_LAST_MONO < 20:
      return web.json_response({'ok':False,'error':'20_second_cooldown','transmitted':False},status=429)
    _DOOR_EXPERIMENT_LAST_MONO=time.monotonic()
    counter=_next_counter()
    frames=_build_frames(action,counter)  # Existing 3FF variant, existing bus 0; NO Safety relaxation.
    rx=messaging.sub_sock('can',conflate=False,timeout=70)
    payloads={data for _,data,_,_ in frames}
    try:
      send=_get_send_sock()  # Preserve existing KO door IPC hotfix.
      for addr,data,bus,delay_ms in frames:
        if delay_ms: await asyncio.sleep(delay_ms/1000)
        send.send(can_list_to_can_capnp([(addr,data,bus)],msgtype='sendcan',valid=True))
      tx=await asyncio.to_thread(_collect_tx_returns,rx,payloads)
      _write_counter(counter)
    except Exception as exc:
      failure={'action':action,'error':'send_failed: '+str(exc),'transmitted':False,
               'door_state_before':before,'live':live}
      _door_test_audit(failure)
      return web.json_response({'ok':False,**failure},status=500)
    await asyncio.sleep(0.5)
    try:
      after=await asyncio.to_thread(_read_ko_door_lock_state)
    except Exception as exc:
      after={'state':'unknown','error':str(exc)}
    confirmed=(tx['status']=='returned' and before.get('corroborated') is True and
               after.get('corroborated') is True and before.get('locked') is not wanted and
               after.get('locked') is wanted)
    result={'ok':bool(confirmed),'action':action,'experimental':True,
            'protocol':'UNVERIFIED_0x3FF','transmitted':tx['status']=='returned',
            'actuator_state_transition_confirmed':bool(confirmed),
            'error':None if confirmed else 'state_change_not_verified',
            'counter':counter,'payloads':[data.hex() for _,data,_,_ in frames],
            'panda_tx':tx,'door_state_before':before,'door_state_after':after,
            'live':live}
    _door_test_audit(result)
    return web.json_response(result,status=200 if confirmed else 409)


async def _door_experiment_log(request: web.Request) -> web.StreamResponse:
  if not _DOOR_EXPERIMENT_LOG.exists():
    return web.json_response({'ok':False,'error':'no tests recorded yet'},status=404)
  return web.FileResponse(_DOOR_EXPERIMENT_LOG,headers={'Content-Disposition':'attachment; filename="door_experiments_v24.jsonl"'})


# KO_DOOR_PROBE_V251: independent RX evidence; no additional vehicle commands.
async def _run_door_experiment(action: str) -> web.Response:
  from . import ko_door_probe
  return await ko_door_probe.record_one_experiment(action, _run_door_experiment_v24)


# KO_DOOR_PARK_EXPERIMENT_20261031: existing guarded, state-confirmed CAN trial.
async def door_command(request: web.Request) -> web.Response:
  action=request.match_info.get('action','').strip().lower()
  if action not in ('lock','unlock'):
    return web.json_response({'ok':False,'error':'invalid_action',
                              'can_transmitted':False},status=400)
  return await _run_door_experiment_v24(action)


# KO_DOOR_DRYRUN_V1_READ_ONLY
# Never sends an IPC request or vehicle CAN frame. Does not toggle any Params.
def _ko_door_read_recent_rx():
  import gzip
  from .ko_door_probe import PROBE_DIR
  files = sorted(PROBE_DIR.glob('door_probe_*.jsonl.gz'),
                 key=lambda q: q.stat().st_mtime_ns, reverse=True)[:4]
  result = []
  for path in files:
    total, pass_count, reject_count = 0, 0, 0
    first_bad = []
    last = None
    try:
      with gzip.open(path, 'rt', encoding='utf-8') as f:
        for line in f:
          if '"id":"3FF"' not in line and '"id": "3FF"' not in line:
            continue
          item = json.loads(line)
          if item.get('id') != '3FF' or item.get('src') != 0:
            continue
          dat = bytes.fromhex(item.get('data', ''))
          if len(dat) != 8:
            continue
          total += 1
          cmd = dat[1] & 15
          state = dat[3]
          ok = (dat[2] == 1 and dat[4:] == bytes(4) and
                ((cmd == 0 and state == 0) or
                 (cmd == 4 and state in (5, 21))) and
                dat[0] == _door_crc8(dat[1:]))
          if ok:
            pass_count += 1
          else:
            reject_count += 1
            if len(first_bad) < 4:
              first_bad.append({'t':item.get('t'), 'data':dat.hex(),
                                'counter':dat[1] >> 4, 'type_nibble':cmd,
                                'state_byte':state})
          last = {'t':item.get('t'), 'data':dat.hex(),
                  'counter':dat[1] >> 4}
    except (OSError, ValueError, UnicodeError) as ex:
      result.append({'file':path.name, 'error':str(ex)[:100]})
      continue
    result.append({'file':path.name, 'bus0_3ff':total, 'passes_card_shape':pass_count,
                   'fails_card_shape':reject_count, 'first_rejected':first_bad,
                   'last_observed':last})
  return result


async def ko_door_dryrun(request: web.Request) -> web.Response:
  action = request.query.get('action', 'lock').strip().lower()
  if action not in ('lock', 'unlock'):
    return web.json_response({'ok':False, 'error':'action must be lock or unlock'}, status=400)
  try:
    live, door_state, examples = await asyncio.gather(
      asyncio.to_thread(_live_state),
      asyncio.to_thread(_read_ko_door_lock_state),
      asyncio.to_thread(_ko_door_read_recent_rx))
  except Exception as ex:
    return web.json_response({'ok':False, 'error':'dryrun_read_failed',
                              'detail':str(ex)[:160], 'can_transmitted':False},status=503)
  params_on = Params().get_bool('KoDoorControlEnabled')
  checks = {
    'toggle_on': params_on,
    'can_valid': live['can_valid'],
    'park': live['park'],
    'standstill': abs(live['v_ego']) <= 0.1,
    'assist_disengaged': not live['engaged'],
    'panda_ignition_and_flag': live['ignition'] and live['door_safety_enabled'],
    'panda_controls_blocked': not live['panda_controls_allowed'],
  }
  stored_counter = _read_counter()
  counter = (stored_counter + 1) & 15
  candidate = _door_payload(action, counter)
  return web.json_response({
    'ok': True, 'version':'KO Door Dry-run V1', 'action':action,
    'can_transmitted':False, 'ipc_message_sent':False,
    'source_edits_during_request':False,
    'all_card_runtime_guards_pass':all(checks.values()),
    'card_runtime_guard_checks':checks,
    'door_state_read_only':door_state,
    'would_change_current_state': (
      door_state.get('corroborated') is True and
      door_state.get('locked') != (action == 'lock')),
    'old_generator_preview': {
      'payload_hex':candidate.hex(), 'stored_last_counter':stored_counter,
      'generated_counter':counter,
      'counter_synced_to_live_vehicle':False,
      'note':'preview only; old generator uses its own stored counter, not the observed vehicle counter'},
    'physical_capture_comparison':examples,
    'critical_limitations':[
      'Existing POST /api/ko/door/action handler is disabled (HTTP 409).',
      'Panda checks include recent gear/wheel CAN frames and cannot be proven by this API.',
      '0x3FF may be feedback, not a door actuation request.',
      'No vehicle command has been transmitted by dry-run.'
    ],
  })


# KO_EGMP_DOOR_V2_PREVIEW_NO_TX: candidate preview accepts ONLY GET; no Panda/cereal used.
async def egmp_door_preview(request: web.Request) -> web.Response:
  action = request.match_info.get('action', '').strip().lower()
  try:
    data = candidate_preview(action)
  except ValueError:
    return web.json_response({'ok': False, 'error': 'invalid_action',
                              'can_transmitted': False}, status=400)
  return web.json_response(data)



# KO_DOOR_SWITCH_COMPARE_V271: receive-only, RAM-only.
def _ko_switch_collect_v271():
  # KO_SWITCH_TRANSITION_V272: 수신 전용, RAM 분석, 파일 저장 없음
  from collections import Counter
  from .ko_door_state import decode_lock_indicator

  periods = (3.0, 6.0, 9.5)
  frames = {}
  states = [
    {0x411: Counter(), 0x414: Counter()}
    for _ in range(3)
  ]
  transitions = []
  last_states = {}
  sock = messaging.sub_sock('can', conflate=False, timeout=75)
  start = time.monotonic()
  received = 0

  while True:
    elapsed = time.monotonic() - start
    if elapsed >= periods[-1]:
      break

    item = messaging.recv_one_or_none(sock)
    if item is None:
      continue

    elapsed = time.monotonic() - start
    if elapsed >= periods[-1]:
      break
    window = 0 if elapsed < 3 else (1 if elapsed < 6 else 2)

    for frame in item.can:
      bus, addr = int(frame.src), int(frame.address)
      if bus not in (0, 1, 2):
        continue
      data = bytes(frame.dat)
      if not 1 <= len(data) <= 64:
        continue
      received += 1

      if bus == 0 and addr in (0x411, 0x414):
        value = decode_lock_indicator(addr, data)
        if value is not None:
          states[window][addr][value] += 1
          previous = last_states.get(addr)
          if (previous is not None and previous != value
                  and 3 <= elapsed <= 7):
            transitions.append({
              't': round(elapsed, 3),
              'id': f'0x{addr:X}',
              'from': 'locked' if previous else 'unlocked',
              'to': 'locked' if value else 'unlocked',
            })
          last_states[addr] = value

      key = (bus, addr)
      if key not in frames:
        if len(frames) >= 1200:
          continue
        frames[key] = {
          'n': [0, 0, 0],
          'samples': [Counter(), Counter(), Counter()],
          'and': [None, None, None],
          'or': [None, None, None],
        }

      rec = frames[key]
      rec['n'][window] += 1
      counter = rec['samples'][window]
      hx = data.hex()
      if hx in counter or len(counter) < 16:
        counter[hx] += 1

      lower, upper = rec['and'][window], rec['or'][window]
      if lower is None:
        rec['and'][window] = bytearray(data)
        rec['or'][window] = bytearray(data)
      elif len(data) == len(lower):
        for i, value in enumerate(data):
          lower[i] &= value
          upper[i] |= value

  def top(counter):
    return counter.most_common(1)[0][0] if counter else None

  def state(index):
    a = states[index][0x411]
    b = states[index][0x414]
    if sum(a.values()) < 2 or sum(b.values()) < 2:
      return 'unknown'
    av, bv = top(a), top(b)
    return ('locked' if av else 'unlocked') if av == bv else 'unknown'

  def stable_bits(rec):
    if rec['n'][0] < 5 or rec['n'][2] < 5:
      return []
    a0, o0 = rec['and'][0], rec['or'][0]
    a2, o2 = rec['and'][2], rec['or'][2]
    if a0 is None or a2 is None or len(a0) != len(a2):
      return []

    changes = []
    for i, (a, b, c, d) in enumerate(zip(a0, o0, a2, o2)):
      stable = (~(a ^ b) & ~(c ^ d)) & 255
      changed = (a ^ c) & stable
      for bit in range(8):
        if changed & (1 << bit):
          changes.append(f'byte{i}.bit{bit}')
    return changes

  ranked = []
  for (bus, addr), rec in frames.items():
    counts = rec['n']
    samples = rec['samples']
    bits = stable_bits(rec)

    event_only = (
      counts[0] == 0 and counts[1] > 0 and counts[2] == 0
    )
    pulse = (
      counts[1] > 0 and counts[0] >= 4 and counts[2] >= 4
      and all(len(x) <= 5 for x in samples)
      and bool(
        set(samples[1]) - set(samples[0]) - set(samples[2])
      )
    )
    if not (bits or event_only or pulse):
      continue

    score = min(48, 5 * len(bits))
    score += 12 if event_only else 0
    score += 4 if pulse else 0
    if addr in (0x3FF, 0x4A2, 0x587, 0x588):
      score += 2

    kind = (
      'stable bit change: ' + ', '.join(bits[:6])
      if bits else (
        'event-only (actuator NOT proven)'
        if event_only else 'event payload variation'
      )
    )
    ranked.append((score, {
      'bus': bus,
      'id': f'0x{addr:X}',
      'type': kind,
      'before': top(samples[0]),
      'event': top(samples[1]),
      'after': top(samples[2]),
      'counts': counts,
    }))

  ranked.sort(
    key=lambda x: (-x[0], x[1]['bus'], x[1]['id'])
  )

  tracked = {}
  for addr in (0x3FF, 0x411, 0x414, 0x4A2, 0x587, 0x588):
    for bus in (0, 1, 2):
      rec = frames.get((bus, addr))
      if rec:
        s = rec['samples']
        tracked[f'{bus}:0x{addr:X}'] = {
          'before': top(s[0]),
          'event': top(s[1]),
          'after': top(s[2]),
          'counts': rec['n'],
          'stable_bits_changed': stable_bits(rec)[:12],
        }

  return {
    'received': received,
    'before': state(0),
    'after': state(2),
    'candidates': [x for _, x in ranked[:15]],
    'tracked': tracked,
    'transition_events': transitions[:16],
    'can_transmitted': False,
    'saved_logs': False,
    'note': 'Passive candidates, not verified actuator commands.',
  }



async def ko_door_switch_compare_v271(
  request: web.Request
) -> web.Response:

  try:
    body = await request.json()
  except Exception:
    body = {}

  if (
    not isinstance(body, dict)
    or body.get('confirmation')
       != 'PHYSICAL_SWITCH_RX_ONLY'
    or body.get('action') not in (
      'lock', 'unlock'
    )
  ):
    return web.json_response(
      {
        'ok': False,
        'error': 'invalid_test_request'
      },
      status=400
    )

  live = await asyncio.to_thread(_live_state)

  if not (
    live.get('ignition')
    and live.get('park')
    and live.get('can_valid')
    and abs(
      float(live.get('v_ego', 999))
    ) < 0.05
    and not live.get('engaged')
    and not live.get('panda_controls_allowed')
  ):
    return web.json_response(
      {
        'ok': False,
        'error': (
          'require_ignition_P_standstill_'
          'CAN_valid_assist_off'
        ),
        'can_transmitted': False
      },
      status=409
    )

  if not hasattr(
    ko_door_switch_compare_v271, '_lock'
  ):
    ko_door_switch_compare_v271._lock = (
      asyncio.Lock()
    )

  lock = ko_door_switch_compare_v271._lock

  if lock.locked():
    return web.json_response(
      {
        'ok': False,
        'error': 'test_already_running',
        'can_transmitted': False
      },
      status=409
    )

  async with lock:
    result = await asyncio.to_thread(
      _ko_switch_collect_v271
    )

  return web.json_response({
    'ok': True,
    'action': body['action'],
    'can_transmitted': False,
    'saved_logs': False,
    **result
  })


# KO_DOOR_EDGE_V290: RX-only, no CAN TX or disk logs.
_KO_EDGE_V290_LOCK = asyncio.Lock()

def _ko_edge_capture_v290(action):
  from collections import Counter, deque, defaultdict
  from .ko_door_state import decode_lock_indicator

  expected = False if action == 'lock' else True
  target = not expected
  samples = deque(maxlen=60000)
  state_counts = {0x411:Counter(), 0x414:Counter()}
  previous = {0x411:None, 0x414:None}
  first_edge = None
  start = time.monotonic()
  sock = messaging.sub_sock('can', conflate=False, timeout=50)
  rx_count = 0

  while time.monotonic()-start < 10.0:
    msg = messaging.recv_one_or_none(sock)
    if msg is None: continue
    t = time.monotonic()-start
    for frame in msg.can:
      bus, addr = int(frame.src), int(frame.address)
      if bus not in (0,1,2): continue
      data = bytes(frame.dat)
      if not 1 <= len(data) <= 64: continue
      rx_count += 1
      samples.append((t,bus,addr,data))
      if bus == 0 and addr in (0x411,0x414) and len(data)==8:
        value = decode_lock_indicator(addr,data)
        if value is None: continue
        if t <= 2.7:
          state_counts[addr][value] += 1
        if t >= 3.0 and previous[addr] == expected and value == target and first_edge is None:
          first_edge = t
        previous[addr] = value

  baseline = [
    state_counts[a].most_common(1)[0][0]
    if state_counts[a] else None
    for a in (0x411,0x414)
  ]
  baseline_ok = baseline == [expected,expected]

  def final_state(addr):
    values = [
      decode_lock_indicator(a,d)
      for t,b,a,d in samples
      if b==0 and a==addr and t>8.0 and len(d)==8
    ]
    values = [v for v in values if v is not None]
    return (
      Counter(values).most_common(1)[0][0]
      if len(values)>=2 else None
    )

  final = [final_state(a) for a in (0x411,0x414)]
  changed = baseline_ok and final==[target,target]
  event_t = (
    first_edge
    if changed and first_edge is not None
    else 5.0
  )
  valid_event = bool(
    changed and first_edge is not None
    and 3.0 <= first_edge <= 7.5
  )

  before = defaultdict(list)
  during = defaultdict(list)
  after = defaultdict(list)
  timelines = defaultdict(list)

  for t,bus,addr,data in samples:
    rel=t-event_t
    key=(bus,addr)

    if -1.4 <= rel < -0.55:
      before[key].append(data)
    elif -0.25 <= rel <= 0.50:
      during[key].append(data)
    elif 0.55 < rel <= 1.40:
      after[key].append(data)

    if (
      -0.55 <= rel <= 0.80
      and addr in (
        0x3FF,0x411,0x414,
        0x4A2,0x587,0x588
      )
    ):
      if len(timelines[key]) < 36:
        timelines[key].append({
          'dt_ms':round(rel*1000),
          'data':data.hex()
        })

  def popular(x):
    return (
      Counter(v.hex() for v in x).most_common(1)[0][0]
      if x else None
    )

  result=[]
  for key in set(before)|set(during)|set(after):
    b,e,a=before[key],during[key],after[key]
    if not e: continue

    event_only = not b and not a
    distinct_event = (
      {v for v in e}
      - {v for v in b}
      - {v for v in a}
    )
    if not event_only and not distinct_event:
      continue

    bus,addr=key
    if addr in (0x411,0x414):
      continue

    score=(15 if event_only else 0)
    score+=min(6,len(distinct_event))
    if addr==0x3FF: score+=3
    if len(e)<2: score-=3

    result.append((score,{
      'bus':bus,
      'id':f'0x{addr:X}',
      'kind':'event-only' if event_only
             else 'event-specific payload',
      'before':popular(b),
      'during':popular(e),
      'after':popular(a),
      'counts':[len(b),len(e),len(a)],
      'event_samples':list(
        dict.fromkeys(v.hex() for v in e)
      )[:8]
    }))

  result.sort(
    key=lambda x:(-x[0],x[1]['bus'],x[1]['id'])
  )

  detail={
    f'{bus}:0x{addr:X}':rows
    for (bus,addr),rows in sorted(timelines.items())
  }

  return {
    'ok':True,
    'action':action,
    'can_transmitted':False,
    'saved_logs':False,
    'baseline':'unlocked' if expected is False else 'locked',
    'final':(
      'locked' if final[0]
      else 'unlocked'
    ) if final[0] is not None and final[0]==final[1]
      else 'unknown',
    'transition_confirmed':changed,
    'edge_triggered':valid_event,
    'reference':(
      'state-transition'
      if valid_event
      else 'scheduled-fallback-NOT-confirmed'
    ),
    'event_at_s':round(event_t,3),
    'frames_received':rx_count,
    'top_candidates':[x for _,x in result[:15]],
    'timeline':detail,
    'note':'RX only; NOT validated actuator commands.'
  }

async def ko_door_edge_v290(request: web.Request) -> web.Response:
  try:
    body=await request.json()
  except Exception:
    body={}

  if (
    not isinstance(body,dict)
    or body.get('confirmation')!='PHYSICAL_SWITCH_RX_ONLY_V290'
    or body.get('action') not in ('lock','unlock')
  ):
    return web.json_response({
      'ok':False,
      'error':'invalid_request',
      'can_transmitted':False
    },status=400)

  if _KO_EDGE_V290_LOCK.locked():
    return web.json_response({
      'ok':False,
      'error':'capture_already_running',
      'can_transmitted':False
    },status=409)

  live=await asyncio.to_thread(_live_state)
  try:
    safe=(
      live.get('ignition') is True
      and live.get('park') is True
      and live.get('can_valid') is True
      and not live.get('engaged')
      and not live.get('panda_controls_allowed')
      and abs(float(live.get('v_ego',999))) <= 0.05
    )
  except (ValueError,TypeError):
    safe=False

  if not safe:
    return web.json_response({
      'ok':False,
      'error':'P_ignition_standstill_CAN_valid_assist_off_required',
      'can_transmitted':False
    },status=409)

  async with _KO_EDGE_V290_LOCK:
    try:
      data=await asyncio.to_thread(
        _ko_edge_capture_v290,body['action']
      )
    except Exception as e:
      return web.json_response({
        'ok':False,
        'error':'rx_capture_failed:'+str(e)[:160],
        'can_transmitted':False
      },status=503)

  return web.json_response(data)

def register(app: web.Application) -> None:
  app.router.add_get("/api/ko/vehicle/status", status)
  app.router.add_post("/api/ko/pet_mode", pet_mode)
  app.router.add_get("/api/ko/door/state", ko_door_state)
  app.router.add_post("/api/ko/door/edge/v290", ko_door_edge_v290)
  app.router.add_post('/api/ko/door/switch/compare', ko_door_switch_compare_v271)
  app.router.add_get("/api/ko/door/candidate/{action}", egmp_door_preview)
  app.router.add_get("/api/ko/door/dryrun", ko_door_dryrun)
  app.router.add_get("/api/ko/door/experiment_log", _door_experiment_log)
  from . import ko_door_probe
  app.router.add_get('/api/ko/door/probe/status', ko_door_probe.get_probe_status)
  app.router.add_get('/api/ko/door/probe/download', ko_door_probe.download_probe)
  app.router.add_get('/api/ko/door/probe/history', ko_door_probe.probe_history)
  app.router.add_post('/api/ko/door/probe/capture', ko_door_probe.physical_capture)
  app.router.add_post("/api/ko/door/{action}", door_command)
