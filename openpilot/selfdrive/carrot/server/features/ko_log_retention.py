"""Bounded KOPilot driving-log retention with analyze-before-eviction.

Only files named KOPilot_drive_*.jsonl in the configured drive log folder
are managed. No other openpilot, panda or camera logs are touched.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

MAX_LOG_BYTES = 100_000_000
TARGET_BYTES = 90_000_000
SEGMENT_BYTES = 8 * 1024 * 1024
INDEX_PATH = Path('/data/ko/auto_tune_rollover_index.json')
LATEST_PATH = Path('/data/ko/auto_tune_rollover_latest.json')
ARCHIVE_DIR = Path('/data/ko/auto_tune_reports')
_LOCK = threading.RLock()


def _atomic_json(path: Path, body: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temp = path.with_name(path.name + '.tmp')
  with temp.open('w', encoding='utf-8') as f:
    json.dump(body, f, ensure_ascii=False, separators=(',', ':'))
    f.flush()
    os.fsync(f.fileno())
  temp.replace(path)


def _index() -> dict:
  try:
    value = json.loads(INDEX_PATH.read_text('utf-8'))
    if isinstance(value, dict) and isinstance(value.get('done'), dict):
      return value
  except (ValueError, OSError, TypeError):
    pass
  return {'version': 1, 'done': {}}


def _logs(log_dir: Path) -> list[Path]:
  # Managed 10Hz JSONL only; no deletion of arbitrary user/device files.
  return sorted((p for p in log_dir.glob('KOPilot_drive_*.jsonl') if p.is_file()),
                key=lambda p: (p.stat().st_mtime_ns, p.name))


def _total(logs: list[Path]) -> int:
  return sum(p.stat().st_size for p in logs if p.is_file())


def _remember(path: Path, result: dict) -> None:
  # Analyzed metrics/proposals survive later removal of source raw samples.
  summary = {'at': time.time(), 'source_file': path.name,
             'state': 'analyzed', 'analysis': result,
             'retention': 'analyze_before_delete', 'automatic_parameter_apply': False}
  good = any(result.get(axis, {}).get('state') == 'analyzed'
             for axis in ('steering', 'deceleration'))
  if good:
    _atomic_json(LATEST_PATH, summary)


def archive_file(path: Path) -> dict[str, Any]:
  """Persist an analysis report before declaring a segment deletable."""
  with _LOCK:
    if not path.is_file():
      raise FileNotFoundError(path)
    index = _index()
    signature = f'{path.stat().st_size}:{path.stat().st_mtime_ns}'
    old = index['done'].get(path.name)
    if isinstance(old, dict) and old.get('signature') == signature:
      return old

    from . import ko_auto_tune  # deferred import avoids circular initialization
    result = ko_auto_tune.analyze_log(path, {'steering': True, 'deceleration': True})
    if result.get('ok') is not True:
      raise RuntimeError('analysis returned non-success')

    # Independent report per source, to preserve original analysis after pruning.
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    report = ARCHIVE_DIR / ('ko_rollover_' + path.stem + '.json')
    _atomic_json(report, result)
    _remember(path, result)
    recorded = {'signature': signature, 'report': report.name,
                'at': time.time(), 'analyzed_samples': result.get('total_log_samples', 0)}
    index['done'][path.name] = recorded
    if len(index['done']) > 1000:
      by_time = sorted(index['done'].items(), key=lambda item: item[1].get('at', 0))
      index['done'] = dict(by_time[-1000:])
    _atomic_json(INDEX_PATH, index)
    return recorded


def _retain_newest_bytes(path: Path, keep: int) -> int:
  """Trim a legacy oversized single file from the beginning, at a line boundary."""
  if keep <= 0:
    return 0
  with path.open('rb') as source:
    source.seek(max(0, path.stat().st_size - keep - 1))
    if source.tell() > 0:
      source.readline()  # discard the partial oldest JSON record
    temp = path.with_name(path.name + '.retention_tmp')
    try:
      with temp.open('xb') as dst:
        shutil.copyfileobj(source, dst, length=1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
      temp.replace(path)
    finally:
      temp.unlink(missing_ok=True)
  return path.stat().st_size


def enforce(log_dir: Path, current: Path | None = None, target: int = TARGET_BYTES) -> dict:
  """Process oldest closed segments, then remove them until under target.

  Failure is fail-closed: never discard unanalyzed raw data to make room.
  The recorder must stop if the quota cannot be met.
  """
  with _LOCK:
    logs = _logs(log_dir)
    total = _total(logs)
    removed = []
    error = None
    for path in logs:
      if total <= target:
        break
      if current is not None and path == current:
        continue
      try:
        archive_file(path)
        size = path.stat().st_size
        budget_for_path = max(0, target - (total - size))
        if budget_for_path > 0 and size > budget_for_path:
          retained_size = _retain_newest_bytes(path, budget_for_path)
          total = total - size + retained_size
          removed.append('TRIMMED_OLDEST_RECORDS:' + path.name)
        else:
          path.unlink()
          total -= size
          removed.append(path.name)
      except Exception as exc:
        error = f'{path.name}: {exc}'
        break
    return {'total_bytes': total, 'max_bytes': MAX_LOG_BYTES,
            'target_bytes': target, 'removed': removed,
            'within_limit': total <= MAX_LOG_BYTES,
            'error': error}


def status(log_dir: Path) -> dict:
  logs = _logs(log_dir)
  total = _total(logs)
  return {'used_bytes': total, 'limit_bytes': MAX_LOG_BYTES,
          'segment_limit_bytes': SEGMENT_BYTES, 'files': len(logs),
          'within_limit': total <= MAX_LOG_BYTES,
          'last_analyzed_file': latest_analysis().get('source_file')}


def latest_analysis() -> dict:
  try:
    return json.loads(LATEST_PATH.read_text(encoding='utf-8'))
  except (ValueError, OSError):
    return {}
