"""Owner-initiated KO Web source publishing, guarded by the existing repo_lock."""
from __future__ import annotations
import json
import os
import re
import subprocess
from pathlib import Path
from openpilot.common.repo_update import child_lock_kwargs

ROOT = "/data/openpilot"
BRANCH = "test"
REMOTE = "origin"
RECORD = Path("/data/ko/git_push_last.json")
SOURCE_ROOTS = ("openpilot", "scripts", "tools", "docs")
SOURCE_EXTS = frozenset((".py", ".pyx", ".pxd", ".js", ".mjs", ".ts",
                         ".css", ".html", ".sh", ".md", ".txt", ".json",
                         ".yaml", ".yml", ".toml", ".c", ".cc", ".cpp",
                         ".h", ".hpp", ".proto"))
SENSITIVE_NAMES = ("secret", "credential", "password", ".env", "id_rsa", "id_ed25519")
# Assemble signatures so our own detector does not match its source code.
SENSITIVE_SIGNATURES = (
  "-----BEGIN " + "PRIVATE KEY-----",
  "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
  "github" + "_pat_",
  "gh" + "p_",
  "AK" + "IA",
)

class GitWebError(RuntimeError):
  pass

def _git(*args: str, timeout: int = 60) -> str:
  env = dict(os.environ)
  env["GIT_TERMINAL_PROMPT"] = "0"
  try:
    p = subprocess.run(["git", *args], cwd=ROOT, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       **child_lock_kwargs())
  except (OSError, subprocess.TimeoutExpired) as exc:
    raise GitWebError(f"git {args[0]} failed: {exc}") from exc
  if p.returncode:
    output = (p.stderr or p.stdout or "")[:950]
    raise GitWebError(f"git {args[0]} failed (code {p.returncode}): {output}")
  return p.stdout.strip()

def _ready(*, require_offroad: bool = False) -> None:
  if _git("symbolic-ref", "--quiet", "--short", "HEAD") != BRANCH:
    raise GitWebError("Git push is restricted to local branch test.")
  url = _git("remote", "get-url", "--push", REMOTE)
  approved = (
    "https://github.com/KeIIog/kopilot33.git",
    "https://github.com/KeIIog/kopilot33",
    "git@github.com:KeIIog/kopilot33.git",
    "git@github.com:KeIIog/kopilot33",
    "ssh://git@github.com/KeIIog/kopilot33.git",
  )
  if url not in approved:
    raise GitWebError("Remote push URL must be KeIIog/kopilot33; refusing other recipients.")
  if require_offroad:
    try:
      from openpilot.common.params import Params
      if Params().get_bool("IsOnroad"):
        raise GitWebError("Undo Push is disabled while onroad.")
    except GitWebError:
      raise
    except Exception:
      raise GitWebError("Cannot determine onroad state; Undo Push blocked.")

def _remote_sha() -> str:
  result = _git("ls-remote", "--heads", REMOTE, "refs/heads/" + BRANCH, timeout=60).split()
  if len(result) != 2 or not re.fullmatch(r"[0-9a-f]{40}", result[0]):
    raise GitWebError("Could not uniquely verify origin/test remote head.")
  return result[0]

def _fetch(expected: str) -> None:
  _git("fetch", "--no-tags", REMOTE,
       f"refs/heads/{BRANCH}:refs/remotes/{REMOTE}/{BRANCH}", timeout=180)
  if _git("rev-parse", f"refs/remotes/{REMOTE}/{BRANCH}") != expected:
    raise GitWebError("origin/test changed while fetching; retry later.")

def _safe_path(path: str) -> bool:
  p = Path(path)
  return (not p.is_absolute() and ".." not in p.parts and len(p.parts) > 1
          and p.parts[0] in SOURCE_ROOTS and p.suffix.lower() in SOURCE_EXTS
          and not any(part in p.name.lower() for part in SENSITIVE_NAMES))

def _write_record(record: dict) -> None:
  RECORD.parent.mkdir(parents=True, exist_ok=True)
  temporary = RECORD.with_suffix(".tmp")
  fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
  with os.fdopen(fd, "w", encoding="utf-8") as out:
    json.dump(record, out)
    out.flush()
    os.fsync(out.fileno())
  os.replace(temporary, RECORD)

def push() -> dict:
  _ready()
  before = _remote_sha()
  _fetch(before)
  # Never blindly use 'git add -A': keep untracked files/logs/private data local.
  _git("add", "-u", "--", *SOURCE_ROOTS)
  # Include the new privacy Git helper file, otherwise an initial clone could not push.
  _git("add", "--", "openpilot/selfdrive/carrot/server/features/tools/git_operations.py", 
       "openpilot/selfdrive/carrot/web/js/ko_git_push.js")
  paths = [p for p in _git("diff", "--cached", "--name-only", "-z").split("\0") if p]
  unsafe = [p for p in paths if not _safe_path(p)]
  if unsafe:
    raise GitWebError("Non-source or sensitive staged files; review via SSH: " + ", ".join(unsafe[:6]))
  if paths:
    # Check additions only, not old lines, context, or diff headers.
    diff = _git("diff", "--cached", "--no-ext-diff", "--unified=0", "--", *paths)
    current_file = "unknown"
    for line in diff.splitlines():
      if line.startswith("+++ b/"):
        current_file = line[6:]
      elif line.startswith("+") and not line.startswith("+++"):
        if any(sig in line[1:] for sig in SENSITIVE_SIGNATURES):
          raise GitWebError("Potential credential in staged additions: " + current_file +
                            ". Inspect securely over SSH; no secret is displayed.")
    _git("commit", "-m", "KOPilot KO Web: save tracked source edits", timeout=90)
  after = _git("rev-parse", "HEAD")
  if after == before:
    return {"ok": True, "out": "Already up-to-date; no new push.", "unchanged": True}
  _git("merge-base", "--is-ancestor", before, after)
  _git("push", REMOTE, f"HEAD:refs/heads/{BRANCH}", timeout=180)
  _write_record({"before": before, "after": after, "reverted": False, "branch": BRANCH})
  return {"ok": True, "before": before, "after": after,
          "out": f"Pushed to origin/test: {before[:12]} -> {after[:12]}. "
                 f"Auto-staged tracked source files: {len(paths)}. Untracked files excluded."}

def undo_push() -> dict:
  _ready(require_offroad=True)
  try:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
  except (OSError, ValueError) as exc:
    raise GitWebError("No earlier successful KO Web push recorded on this device.") from exc
  before, after = str(record.get("before", "")), str(record.get("after", ""))
  if (record.get("reverted") or record.get("branch") != BRANCH or
      not all(re.fullmatch(r"[0-9a-f]{40}", x) for x in (before, after))):
    raise GitWebError("Already reverted or invalid push record.")
  if _remote_sha() != after or _git("rev-parse", "HEAD") != after:
    raise GitWebError("Remote/local branch changed after the recorded push; undo blocked.")
  if _git("status", "--porcelain", "--untracked-files=no"):
    raise GitWebError("Tracked local changes exist; save them before undo.")
  _fetch(after)
  _git("merge-base", "--is-ancestor", before, after)
  # Creates a NEW inverse commit; never force-push/rewrite public history.
  # If a conflict occurs, nothing is pushed. Resolve it over SSH.
  _git("revert", "--no-commit", f"{before}..{after}", timeout=120)
  if not _git("diff", "--cached", "--name-only"):
    raise GitWebError("No revert changes staged; no remote update.")
  _git("commit", "-m", "KOPilot KO Web: revert last button push", timeout=90)
  reverted = _git("rev-parse", "HEAD")
  _git("push", REMOTE, f"HEAD:refs/heads/{BRANCH}", timeout=180)
  record.update({"reverted": True, "revert_commit": reverted})
  _write_record(record)
  return {"ok": True, "revert_commit": reverted,
          "out": f"Last KO Web push reverted by a new commit: {reverted[:12]}. No force push."}
