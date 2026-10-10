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
from . import ko_drive_log

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
  if path.stat().st_size > 100*1024*1024:
    raise ValueError('log over 100 MB; stop logging and choose shorter session')
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
    'ok': True, 'mode': 'shadow_analysis_only', 'config': conf, 'source_file': path.name,
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
  return web.json_response({'ok':True,'version':'v2.3','config':config,'mode':'shadow_analysis_only',
                            'automatic_parameter_apply':False,'can_transmitted':False,'log':log})


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
    report = await asyncio.to_thread(_save_report,result)
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
