"""KO steering/deceleration auto-analysis (advisory only).

Does not modify live steering or longitudinal controller parameters.
Uses existing 10 Hz KOPilot drive-log samples. Never transmits CAN.
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

from aiohttp import web
from . import ko_drive_log

CONFIG_FILE = Path('/data/ko/auto_tune.json')
DEFAULT = {'steering': False, 'deceleration': False}


def read_config() -> dict[str, bool]:
  try:
    raw = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
      return dict(DEFAULT)
    return {key: raw.get(key) is True for key in DEFAULT}
  except (OSError, ValueError, TypeError):
    return dict(DEFAULT)


def update_config(body: dict[str, Any]) -> dict[str, bool]:
  if not isinstance(body, dict) or not any(key in body for key in DEFAULT):
    raise ValueError('steering and/or deceleration boolean required')
  result = read_config()
  for key in DEFAULT:
    if key in body:
      if not isinstance(body[key], bool):
        raise ValueError(f'{key} must be true/false')
      result[key] = body[key]
  CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
  tmp = CONFIG_FILE.with_suffix('.tmp')
  tmp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
  tmp.replace(CONFIG_FILE)
  return result


def _finite_number(value: Any) -> float | None:
  try:
    x = float(value)
    return x if math.isfinite(x) else None
  except (ValueError, TypeError):
    return None


def _axis(samples: list[tuple[float, float]], min_samples: int, units: str) -> dict:
  count = len(samples)
  if count < min_samples:
    return {'state': 'insufficient_data', 'samples': count, 'required_samples': min_samples,
            'message': '주행 로그 데이터 부족. 로그 기록 ON 후 충분한 주행 데이터가 필요합니다.'}
  # A steady straight line or a constant gentle coast does not identify gains.
  targets = [target for target, _ in samples]
  span = max(targets) - min(targets)
  min_span = 0.75 if units == '1/km' else 0.15
  if span < min_span:
    return {'state': 'insufficient_excitation', 'samples': count,
            'message': '제어 목표 변화가 부족해 튜닝 값을 추정할 수 없습니다.'}
  delta = [actual - target for target, actual in samples]
  return {
    'state': 'analyzed', 'samples': count, 'unit': units,
    'rms_error': round(math.sqrt(mean([x * x for x in delta])), 5),
    'mean_error': round(mean(delta), 5),
    'mean_absolute_error': round(mean([abs(x) for x in delta]), 5),
    'message': '진단 지표만 계산했습니다. 제어 계수 자동 변경은 수행하지 않았습니다.',
  }


def analyze_log(path: Path, config: dict[str, bool] | None = None) -> dict:
  config = read_config() if config is None else config
  result: dict[str, Any] = {
    'ok': True, 'mode': 'advisory_only', 'automatic_parameter_apply': False,
    'can_transmitted': False, 'config': config, 'source_file': path.name,
    'steering': {'state': 'disabled'}, 'deceleration': {'state': 'disabled'},
  }
  if not path.is_file():
    raise FileNotFoundError('no KOPilot drive log available')
  if path.stat().st_size > 100 * 1024 * 1024:
    raise ValueError('drive log exceeds 100 MB; export smaller session')
  steering: list[tuple[float, float]] = []
  decel: list[tuple[float, float]] = []
  prev_time = -1.0
  with path.open('r', encoding='utf-8') as f:
    for line in f:
      try:
        sample = json.loads(line)
      except (ValueError, TypeError):
        continue
      if sample.get('type') != 'sample':
        continue
      t = _finite_number(sample.get('t'))
      if t is None or t < prev_time:
        continue
      prev_time = t
      if not sample.get('carState_valid', False) or not sample.get('selfdriveState_valid', False):
        continue
      car = sample.get('carState') or {}
      sd = sample.get('selfdriveState') or {}
      ctrl = sample.get('controlsState') or {}
      command = sample.get('carControl') or {}
      if not isinstance(car, dict) or not isinstance(sd, dict) or not sd.get('active'):
        continue
      speed = _finite_number(car.get('vEgo'))
      if speed is None or speed < 5 or speed > 55:
        continue
      if config.get('steering') and sample.get('controlsState_valid', False):
        desired = _finite_number(ctrl.get('desiredCurvature'))
        yaw = _finite_number(car.get('yawRate'))
        if desired is not None and yaw is not None and speed > 8 and abs(desired) < .025 and abs(yaw / speed) < .025:
          # Compare road curvature, not actuator torque or steering angles.
          steering.append((desired * 1000, yaw / speed * 1000))
      if config.get('deceleration') and sample.get('carControl_valid', False):
        act = command.get('actuators') or {} if isinstance(command, dict) else {}
        wanted = _finite_number(act.get('accel') if isinstance(act, dict) else None)
        actual = _finite_number(car.get('aEgo'))
        if wanted is not None and actual is not None and -3.5 < wanted < -.3 and -6 < actual < 3:
          decel.append((wanted, actual))
  if config.get('steering'):
    result['steering'] = _axis(steering, 150, '1/km')
  if config.get('deceleration'):
    result['deceleration'] = _axis(decel, 50, 'm/s^2')
  return result


async def status(request: web.Request) -> web.Response:
  return web.json_response({
    'ok': True, 'config': read_config(), 'mode': 'advisory_only',
    'automatic_parameter_apply': False, 'can_transmitted': False,
    'log': ko_drive_log._status_payload(),
  })


async def configure(request: web.Request) -> web.Response:
  try:
    body = await request.json()
    config = await asyncio.to_thread(update_config, body)
  except (ValueError, TypeError) as exc:
    return web.json_response({'ok': False, 'error': str(exc)}, status=400)
  return web.json_response({'ok': True, 'config': config,
                            'mode': 'advisory_only', 'automatic_parameter_apply': False})


async def analyze(request: web.Request) -> web.Response:
  path = ko_drive_log._latest_log()
  if path is None:
    return web.json_response({'ok': False, 'error': 'no drive log; turn on KO drive logging first'}, status=404)
  try:
    result = await asyncio.to_thread(analyze_log, path)
  except (OSError, ValueError) as exc:
    return web.json_response({'ok': False, 'error': str(exc)}, status=400)
  return web.json_response(result)


def register(app: web.Application) -> None:
  app.router.add_get('/api/ko/auto_tune/status', status)
  app.router.add_post('/api/ko/auto_tune/config', configure)
  app.router.add_post('/api/ko/auto_tune/analyze', analyze)
