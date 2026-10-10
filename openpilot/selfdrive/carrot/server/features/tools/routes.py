import asyncio
import ipaddress
from urllib.parse import urlsplit
import time
import uuid

from aiohttp import web

from ...services.git_state import read_auto_update_state
from ...services.git_status import get_git_status
from ...services.device_info import get_tools_device_info
from . import jobs
from .actions import validate_action
from .dispatcher import dispatch_sync, run_tool_job


def _local_git_write_allowed(request: web.Request, action: object) -> bool:
  if str(action or "") not in ("git_push", "git_push_undo"):
    return True
  origin = request.headers.get("Origin", "")
  if origin:
    parsed = urlsplit(origin)
    same_origin = parsed.scheme in ("http", "https") and parsed.netloc == request.host
  else:
    same_origin = request.headers.get("Sec-Fetch-Site") == "same-origin"
  try:
    address = ipaddress.ip_address(request.remote or "")
    private_peer = address.is_private or address.is_loopback
  except ValueError:
    private_peer = False
  return bool(same_origin and private_peer)


async def api_tools_start(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "invalid json"}, status=400)

  action = body.get("action")
  if not _local_git_write_allowed(request, action):
    return web.json_response({"ok": False, "error": "Git writes require a local same-origin browser"}, status=403)
  action_error = validate_action(action)
  if action_error:
    error, error_code = action_error
    return web.json_response({"ok": False, "error": error, "error_code": error_code}, status=400)

  job_id = uuid.uuid4().hex[:12]
  job = {
    "id": job_id,
    "action": str(action),
    "payload": dict(body),
    "status": "running",
    "log": "",
    "progress": 0,
    "message": "",
    "step_current": 0,
    "step_total": 0,
    "error": None,
    "error_code": None,
    "error_detail": None,
    "result": None,
    "created_at": time.time(),
    "updated_at": time.time(),
  }
  jobs.jobs()[job_id] = job
  jobs.prune()
  jobs.persist_now()
  asyncio.create_task(run_tool_job(job))
  return web.json_response({"ok": True, "job_id": job_id, "status": job["status"]})


async def api_tools_job(request: web.Request) -> web.Response:
  job_id = (request.query.get("id") or request.match_info.get("job_id") or "").strip()
  if not job_id:
    return web.json_response({"ok": False, "error": "missing job id"}, status=400)

  job = jobs.jobs().get(job_id)
  if not job:
    return web.json_response({"ok": False, "error": "job not found"}, status=404)

  return web.json_response(jobs.snapshot(job))


async def api_tools_jobs(request: web.Request) -> web.Response:
  try:
    limit = int((request.query.get("limit") or "").strip() or jobs.TOOL_JOB_KEEP_COUNT)
  except Exception:
    limit = jobs.TOOL_JOB_KEEP_COUNT
  limit = max(1, min(jobs.TOOL_JOB_KEEP_COUNT, limit))
  return web.json_response({"ok": True, "jobs": jobs.list_snapshots(limit)})


async def api_tools_jobs_clear(request: web.Request) -> web.Response:
  removed = jobs.clear_finished()
  return web.json_response({"ok": True, "removed": removed, "jobs": jobs.list_snapshots()})


async def api_tools_jobs_notice(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "invalid json"}, status=400)
  message = str(body.get("message") or "").strip()
  if not message:
    return web.json_response({"ok": False, "error": "missing message"}, status=400)
  action = str(body.get("action") or "notice").strip() or "notice"
  job = jobs.add_notice(action, message, {"notice": True})
  return web.json_response({"ok": True, "job": job, "jobs": jobs.list_snapshots()})


async def api_tools(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "invalid json"}, status=400)
  if not _local_git_write_allowed(request, body.get("action")):
    return web.json_response({"ok": False, "error": "Git writes require a local same-origin browser"}, status=403)
  return await dispatch_sync(request, body)


async def api_tools_git_status(request: web.Request) -> web.Response:
  force = any(
    (request.query.get(name) or "").strip().lower() in ("1", "true", "yes")
    for name in ("force", "refresh")
  )
  status = await get_git_status(force=force)
  return web.json_response({"ok": True, **status, "auto_update": read_auto_update_state()})


async def api_tools_device_info(_request: web.Request) -> web.Response:
  """Read the local, non-setting support metadata used by Tools > Info."""
  return web.json_response({"ok": True, "info": await asyncio.to_thread(get_tools_device_info)})


def register(app: web.Application) -> None:
  jobs.load_persisted()
  app.router.add_post("/api/tools", api_tools)
  app.router.add_post("/api/tools/start", api_tools_start)
  app.router.add_get("/api/tools/job", api_tools_job)
  app.router.add_get("/api/tools/jobs", api_tools_jobs)
  app.router.add_delete("/api/tools/jobs", api_tools_jobs_clear)
  app.router.add_post("/api/tools/jobs/notice", api_tools_jobs_notice)
  app.router.add_get("/api/tools/git_status", api_tools_git_status)
  app.router.add_get("/api/tools/device_info", api_tools_device_info)
