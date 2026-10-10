"""KOPilot steering/deceleration response identification in shadow mode.

Automatically records 10Hz logs while enabled, identifies lag/bias tracking,
never adjusts live control parameters or sends vehicle CAN messages.
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from aiohttp import web
from openpilot.cereal import car, messaging
from openpilot.common.params import Params
from . import ko_drive_log, ko_log_retention  # KO_ROLLING_100MB_V25

CONFIG_FILE = Path('/data/ko/auto_tune.json')
REPORT_DIR = Path('/data/ko/auto_tune_reports')
DEFAULT = {'steering': False, 'deceleration': False}
_LOG_LOCK = threading.Lock()
_LOG_OWNED = False


def read_config() -> dict[str, bool]:
  try:
    value = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    return {key: value.get(key) is True for key in DEFAULT} if isinstance(value, dict) else dict(DEFAULT)
  except (OSError, ValueError, TypeError):
    return dict(DEFAULT)


def update_config(body: Any) -> dict[str, bool]:
  if not isinstance(body, dict) or not any(k in body for k in DEFAULT):
    raise ValueError('steering/deceleration boolean required')
  config = read_config()
  for key in DEFAULT:
    if key in body:
      if not isinstance(body[key], bool):
        raise ValueError(key + ' must be boolean')
      config[key] = body[key]
  CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
  temporary = CONFIG_FILE.with_suffix('.tmp')
  temporary.write_text(json.dumps(config), encoding='utf-8')
  temporary.replace(CONFIG_FILE)
  return config


def _sync_logging(config: dict[str, bool]) -> dict[str, Any]:
  """Start/stop logging without interrupting manually started sessions."""
  global _LOG_OWNED
  with _LOG_LOCK:
    active = ko_drive_log._status_payload().get('enabled', False)
    if any(config.values()) and not active:
      ko_drive_log._start_logging()
      _LOG_OWNED = True
    elif not any(config.values()) and _LOG_OWNED and active:
      ko_drive_log._stop_logging()
      _LOG_OWNED = False
    return ko_drive_log._status_payload()


def _number(raw: Any) -> float | None:
  try:
    value = float(raw)
    return value if math.isfinite(value) else None
  except (TypeError, ValueError):
    return None


def _axis(samples: list[tuple[float, float, float]], minimum: int, unit: str) -> dict[str, Any]:
  n = len(samples)
  if n < minimum:
    return {'state': 'insufficient_data', 'samples': n, 'required_samples': minimum}
  span = max(p[1] for p in samples) - min(p[1] for p in samples)
  if span < (0.75 if unit == '1/km' else 0.15):
    return {'state': 'insufficient_excitation', 'samples': n, 'target_span': round(span, 4)}
  errors = [actual - target for _, target, actual in samples]
  abs_err = sorted(abs(e) for e in errors)
  # Indicative lag only; do not infer PID gains or motor transfer functions from it.
  best_lag = None
  best_mse = float('inf')
  for lag in range(7):
    paired = [(samples[i][1], samples[i+lag][2]) for i in range(n-lag)
              if samples[i+lag][0] - samples[i][0] <= 0.17 * (lag + 1)]
    if len(paired) < minimum // 2:
      continue
    mse = mean((a-b)**2 for a,b in paired)
    if mse < best_mse:
      best_mse, best_lag = mse, lag
  return {
    'state': 'analyzed', 'samples': n, 'unit': unit,
    'rms_error': round(math.sqrt(mean(e*e for e in errors)), 5),
    'mean_error': round(mean(errors), 5),
    'p95_absolute_error': round(abs_err[min(n-1, int(0.95*n))], 5),
    'estimated_lag_ms': None if best_lag is None else best_lag * 100,
    'lag_confidence': 'exploratory_only',
    'note': '제어 계수 적용 금지: 단순 추종오차/지연 후보 분석값이며 차량·도로·노면 영향을 포함합니다.',
  }


def analyze_log(path: Path, config: dict[str, bool] | None = None) -> dict[str, Any]:
  conf = config if config is not None else read_config()
  if not path.is_file():
    raise FileNotFoundError('drive log not found')
  # Legacy oversized files are streamed once into a durable report before removal.
  steer: list[tuple[float,float,float]] = []
  decel: list[tuple[float,float,float]] = []
  prev = -1.0
  count = 0
  with path.open('r',encoding='utf-8') as f:
    for line in f:
      try:
        sample = json.loads(line)
      except (ValueError, TypeError):
        continue
      if sample.get('type') != 'sample':
        continue
      count += 1
      t = _number(sample.get('t'))
      if t is None or t < prev:
        continue
      prev = t
      if not sample.get('carState_valid') or not sample.get('selfdriveState_valid'):
        continue
      state = sample.get('carState') or {}
      selfdrive = sample.get('selfdriveState') or {}
      if not isinstance(state,dict) or not isinstance(selfdrive,dict) or not selfdrive.get('active'):
        continue
      speed = _number(state.get('vEgo'))
      if speed is None or not 5 <= speed <= 55:
        continue
      if conf.get('steering') and speed > 8 and sample.get('controlsState_valid'):
        ctr = sample.get('controlsState') or {}
        target = _number(ctr.get('desiredCurvature')) if isinstance(ctr,dict) else None
        yaw = _number(state.get('yawRate'))
        if target is not None and yaw is not None and abs(target)<.025 and abs(yaw/speed)<.025:
          steer.append((t,target*1000,yaw/speed*1000))
      if conf.get('deceleration') and sample.get('carControl_valid'):
        cc = sample.get('carControl') or {}
        actuators = cc.get('actuators') or {} if isinstance(cc,dict) else {}
        wanted = _number(actuators.get('accel')) if isinstance(actuators,dict) else None
        actual = _number(state.get('aEgo'))
        if wanted is not None and actual is not None and -3.5 < wanted < -.3 and -6 < actual < 3:
          decel.append((t,wanted,actual))
  result = {
    'ok': True, 'mode': 'bounded_parked_trial_ready', 'config': conf, 'source_file': path.name,
    'total_log_samples': count, 'automatic_parameter_apply': False, 'can_transmitted': False,
    'steering': _axis(steer,150,'1/km') if conf.get('steering') else {'state':'disabled'},
    'deceleration': _axis(decel,50,'m/s^2') if conf.get('deceleration') else {'state':'disabled'},
    'safety': 'DO NOT automatically apply steering/brake gain changes from these estimates',
  }
  return result


def _save_report(result: dict[str,Any]) -> Path:
  REPORT_DIR.mkdir(parents=True,exist_ok=True)
  path = REPORT_DIR / ('ko_tune_report_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.json')
  path.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
  return path


async def status(request: web.Request) -> web.Response:
  config = read_config()
  log = await asyncio.to_thread(_sync_logging, config)
  return web.json_response({'ok':True,'version':'v2.4','config':config,'mode':'bounded_parked_trial',
                            'automatic_parameter_apply':False,'can_transmitted':False,'log':log,
                            'active_trial':_read_trial(), 'last_retained_analysis':ko_log_retention.latest_analysis().get('source_file'), 'retention':ko_log_retention.status(ko_drive_log.LOG_DIR)})


async def configure(request: web.Request) -> web.Response:
  try:
    body = await request.json()
    config = await asyncio.to_thread(update_config, body)
    log = await asyncio.to_thread(_sync_logging, config)
  except (ValueError, TypeError) as exc:
    return web.json_response({'ok':False,'error':str(exc)},status=400)
  except Exception as exc:
    return web.json_response({'ok':False,'error':'log_start_failed: '+str(exc)},status=500)
  return web.json_response({'ok':True,'config':config,'log':log,'automatic_parameter_apply':False})


async def analyze(request: web.Request) -> web.Response:
  path = ko_drive_log._latest_log()
  if path is None:
    return web.json_response({'ok':False,'error':'no driving log: enable steering/deceleration first'},status=404)
  try:
    result = await asyncio.to_thread(analyze_log,path)
    cached = ko_log_retention.latest_analysis().get('analysis')
    if isinstance(cached, dict):
      for _axis in ('steering', 'deceleration'):
        if (result.get(_axis, {}).get('state') != 'analyzed' and
            cached.get(_axis, {}).get('state') == 'analyzed'):
          result[_axis] = dict(cached[_axis])
          result[_axis]['source_file'] = cached.get('source_file')
    report = await asyncio.to_thread(_save_report,result)
    # Keep analysis alongside retention; original raw log is not required after summary.
    if path != Path(ko_drive_log._state.get('path') or ''):
      await asyncio.to_thread(ko_log_retention.archive_file,path)
    result['report_file'] = report.name
  except (OSError,ValueError) as exc:
    return web.json_response({'ok':False,'error':str(exc)},status=400)
  return web.json_response(result)


async def report(request: web.Request) -> web.StreamResponse:
  files=sorted(REPORT_DIR.glob('ko_tune_report_*.json'),reverse=True) if REPORT_DIR.exists() else []
  if not files:
    return web.json_response({'ok':False,'error':'no saved report'},status=404)
  return web.FileResponse(files[0],headers={'Content-Disposition':'attachment; filename="'+files[0].name+'"'})


def register(app: web.Application) -> None:
  app.router.add_get('/api/ko/auto_tune/status',status)
  app.router.add_post('/api/ko/auto_tune/config',configure)
  app.router.add_post('/api/ko/auto_tune/analyze',analyze)
  app.router.add_get('/api/ko/auto_tune/report',report)
  app.router.add_get('/api/ko/auto_tune/proposal',proposal)
  app.router.add_post('/api/ko/auto_tune/apply_trial',apply_trial)
  app.router.add_post('/api/ko/auto_tune/rollback_trial',rollback_trial)


# v2.4: bounded, explicit, parked-only one-step control parameter trials.
# Steering/longitudinal **controller gain tuning** remains untouched. This
# adjusts only the existing delay Params after user confirmation and with undo.
TRIAL_PATH = Path('/data/ko/auto_tune_trial.json')
TRIAL_EVENTS = Path('/data/ko/auto_tune_trial_events.jsonl')
_TRIAL_MUTEX = threading.Lock()
_ALLOWED = {
  'steering': ('SteerActuatorDelay', 25, 35, 1),
  'deceleration': ('LongActuatorDelay', 15, 25, 5),
}


def _atomic_json(path: Path, payload: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_suffix('.tmp')
  tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
  tmp.replace(path)


def _audit(name: str, info: dict) -> None:
  TRIAL_EVENTS.parent.mkdir(parents=True, exist_ok=True)
  with TRIAL_EVENTS.open('a',encoding='utf-8') as f:
    f.write(json.dumps({'at':time.time(),'event':name,**info},ensure_ascii=False)+'\n')


def _read_trial() -> dict | None:
  try:
    t=json.loads(TRIAL_PATH.read_text(encoding='utf-8'))
    return t if isinstance(t,dict) and t.get('active') is True else None
  except (OSError,ValueError):
    return None


def _parked_ready() -> dict:
  # Never apply active tuning while moving, engaged, or with stale car state.
  sm=messaging.SubMaster(['carState','selfdriveState','carControl','pandaStates'])
  deadline=time.monotonic()+2
  while time.monotonic()<deadline:
    sm.update(100)
    if all(sm.seen.get(x,False) for x in ('carState','selfdriveState','carControl','pandaStates')):
      break
  if not all(sm.seen.get(x,False) for x in ('carState','selfdriveState','carControl','pandaStates')):
    raise ValueError('fresh vehicle state unavailable')
  cs,ss,cc=sm['carState'],sm['selfdriveState'],sm['carControl']
  if cs.gearShifter!=car.CarState.GearShifter.park or abs(cs.vEgo)>0.05 or not cs.canValid:
    raise ValueError('vehicle must be in P, stationary and CAN valid')
  if ss.active or getattr(cc,'latActive',False) or getattr(cc,'longActive',False):
    raise ValueError('disable driving assistance before applying')
  if not any(p.ignitionLine or p.ignitionCan for p in sm['pandaStates']):
    raise ValueError('ignition must be on for fresh parked-state check')
  return {'park':True,'v_ego':float(cs.vEgo),'engaged':False}


def _trial_proposal() -> dict:
  if _read_trial():
    return {'ok':True,'can_apply':False,'reason':'previous trial active; rollback or evaluate it first','active_trial':_read_trial()}
  latest=ko_drive_log._latest_log()
  if latest is None:
    return {'ok':True,'can_apply':False,'reason':'no driving log yet'}
  result=analyze_log(latest)
  saved = ko_log_retention.latest_analysis().get('analysis')
  if isinstance(saved, dict):
    for _axis in ('steering', 'deceleration'):
      if (result.get(_axis, {}).get('state') != 'analyzed' and
          saved.get(_axis, {}).get('state') == 'analyzed'):
        result[_axis] = dict(saved[_axis])
        result[_axis]['source_file'] = saved.get('source_file')
  params=Params()
  proposed={}
  reasons={}
  for key,(param,low,high,step) in _ALLOWED.items():
    axis=result[key]
    if axis.get('state')!='analyzed':
      reasons[key]=axis.get('state','disabled');continue
    lag=axis.get('estimated_lag_ms')
    # At 10 Hz, fewer than 100 ms cannot be resolved. Only a 1-step trial.
    if lag is None or lag<100:
      reasons[key]='lag not resolvable / no correction needed';continue
    current=int(params.get_int(param))
    if not low<=current<=high:
      reasons[key]='current value outside conservative trial window';continue
    if current+step>high:
      reasons[key]='trial cap reached';continue
    proposed[key]={'param':param,'before':current,'after':current+step,
                   'step':step,'lag_observed_ms':lag,'samples':axis.get('samples'),
                   'rms_before':axis.get('rms_error')}
  return {'ok':True,'can_apply':bool(proposed),'source_file':latest.name,
          'proposals':proposed,'skipped':reasons,
          'note':'Experimental one-step anticipatory-delay adjustment, not a certified PID/autotune result. User confirmation and parked state required.'}


async def proposal(request: web.Request) -> web.Response:
  try:
    return web.json_response(await asyncio.to_thread(_trial_proposal))
  except Exception as exc:
    return web.json_response({'ok':False,'error':str(exc)},status=409)


def _apply_trial(body: dict) -> dict:
  if not isinstance(body,dict) or body.get('confirmation')!='APPLY_ONE_STEP_WHILE_PARKED':
    raise ValueError('explicit confirmation is required')
  axis=body.get('axis')
  if axis not in _ALLOWED:
    raise ValueError('axis must be steering or deceleration')
  with _TRIAL_MUTEX:
    state=_parked_ready()
    p=_trial_proposal()
    info=p.get('proposals',{}).get(axis)
    if info is None:
      raise ValueError('no qualified proposal: '+str(p.get('skipped',{}).get(axis,p.get('reason'))))
    params=Params()
    before=int(params.get_int(info['param']))
    if before!=info['before']:
      raise ValueError('value changed since proposal; retry')
    record={'active':True,'axis':axis,'param':info['param'],'before':before,
            'after':info['after'],'source':p['source_file'],
            'at':time.time(),'vehicle_state':state}
    # Persist rollback before updating the controller input.
    _atomic_json(TRIAL_PATH,record)
    try:
      params.put_int(info['param'],int(info['after']))
    except Exception:
      TRIAL_PATH.unlink(missing_ok=True)
      raise
    _audit('trial_apply',record)
    return {'ok':True,'trial':record,'message':'Parked-only one-step trial applied; compare on next supervised drive; rollback is available.'}


async def apply_trial(request: web.Request) -> web.Response:
  try:
    body=await request.json()
    return web.json_response(await asyncio.to_thread(_apply_trial,body))
  except Exception as exc:
    return web.json_response({'ok':False,'error':str(exc)},status=409)


def _rollback_trial(body: dict) -> dict:
  if not isinstance(body,dict) or body.get('confirmation')!='ROLLBACK_WHILE_PARKED':
    raise ValueError('explicit rollback confirmation required')
  with _TRIAL_MUTEX:
    _parked_ready()
    trial=_read_trial()
    if trial is None:
      raise ValueError('no active trial')
    params=Params()
    current=int(params.get_int(trial['param']))
    if current!=int(trial['after']):
      raise ValueError('param was edited externally; refusing to overwrite, manual review required')
    params.put_int(trial['param'],int(trial['before']))
    record={**trial,'active':False,'rolled_back_at':time.time()}
    _atomic_json(TRIAL_PATH,record)
    _audit('trial_rollback',record)
    return {'ok':True,'restored':{trial['param']:trial['before']}}


async def rollback_trial(request: web.Request) -> web.Response:
  try:
    body=await request.json()
    return web.json_response(await asyncio.to_thread(_rollback_trial,body))
  except Exception as exc:
    return web.json_response({'ok':False,'error':str(exc)},status=409)
