#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import py_compile
import shutil
import subprocess
from pathlib import Path

BASE_BRANCH = "ko-wip"
BASE_SHA = "e4d48a534f53fb5c32ac477780d9846a856c3e3b"
NEW_BRANCH = "test"

HERE = Path(__file__).resolve().parent
V15_PATH = HERE / "apply_ko_monitor_v1_5.py"
DOOR_CANDIDATE = HERE / "KO_DOOR_CANDIDATE.json"

spec = importlib.util.spec_from_file_location("kopilot_v15", V15_PATH)
if spec is None or spec.loader is None:
  raise RuntimeError(f"cannot import {V15_PATH}")
v15 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v15)

run = v15.run
output = v15.output
read = v15.read
write = v15.write
replace_once = v15.replace_once


def replace_text(path: Path, replacements: list[tuple[str, str]]) -> None:
  s = read(path)
  out = s
  for old, new in replacements:
    out = out.replace(old, new)
  if out != s:
    write(path, out)


def patch_branding(repo: Path) -> None:
  # Display translations: replace user-visible legacy branding, while keeping
  # internal package names / Params / APIs intact for compatibility.
  ko = repo / "openpilot/selfdrive/carrot/web/js/translations/ko.js"
  replace_text(ko, [
    ("당근비전", "KO Vision"),
    ("당근 비전", "KO Vision"),
    ("당근네비", "KO Navi"),
    ("당근내비", "KO Navi"),
    ("당근 네비", "KO Navi"),
    ("당근맵", "KO Map"),
    ("당근 서버", "KO 서버"),
    ("당근서버", "KO 서버"),
    ("당근파일럿", "KOPilot"),
    ("당근 파일럿", "KOPilot"),
    ("당근 크루즈", "KO 크루즈"),
    ("당근크루즈", "KO 크루즈"),
    ("당근 레이더", "KO 레이더"),
    ("당근", "KO"),
    ("tmux carrot-terminal", "tmux kopilot-terminal"),
  ])

  en = repo / "openpilot/selfdrive/carrot/web/js/translations/en.js"
  replace_text(en, [
    ("CarrotPilot", "KOPilot"),
    ("Carrot Vision", "KO Vision"),
    ("Carrot Navi", "KO Navi"),
    ("Carrot Radar", "KO Radar"),
    ("Carrot Cruise", "KO Cruise"),
    ("Carrot map", "KO Map"),
    ("Carrot server", "KO server"),
    ("Carrot driving mode", "KO driving mode"),
    ("Carrot command", "KO command"),
    ("Carrot speed control", "KO speed control"),
    ('carrot: "Carrot"', 'carrot: "KO"'),
    ('carrot_info: "Carrot Info"', 'carrot_info: "KOPilot Info"'),
    ("tmux carrot-terminal", "tmux kopilot-terminal"),
  ])

  zh = repo / "openpilot/selfdrive/carrot/web/js/translations/zh.js"
  replace_text(zh, [
    ("CarrotPilot", "KOPilot"),
    ("Carrot Vision", "KO Vision"),
    ("Carrot Navi", "KO Navi"),
    ("Carrot 导航", "KO 导航"),
    ("Carrot 地图", "KO 地图"),
    ("Carrot 服务器", "KO 服务器"),
    ("Carrot驾驶", "KO驾驶"),
    ("Carrot命令", "KO命令"),
    ("Carrot速度", "KO速度"),
    ("胡萝卜导航", "KO 导航"),
    ('carrot: "胡萝卜"', 'carrot: "KO"'),
    ('carrot_info: "Carrot Info"', 'carrot_info: "KOPilot Info"'),
    ("tmux carrot-terminal", "tmux kopilot-terminal"),
  ])

  # Recovery / transient web screen that was still visibly branded.
  static_py = repo / "openpilot/selfdrive/carrot/server/features/static.py"
  s = read(static_py)
  s = s.replace("<title>Carrot Web</title>", "<title>KO Web</title>")
  s = s.replace("Carrot Web 업데이트 적용 중", "KO Web 업데이트 적용 중")
  old_bootstrap = "script = f'<script id=\"carrotBootstrap\">window.__CARROT_BOOTSTRAP__ = {payload};</script>\\n'"
  new_bootstrap = "script = f'<script id=\"kopilotBootstrap\">window.__KOPILOT_BOOTSTRAP__ = {payload}; window.__CARROT_BOOTSTRAP__ = window.__KOPILOT_BOOTSTRAP__;</script>\\n'"
  if old_bootstrap in s:
    s = s.replace(old_bootstrap, new_bootstrap, 1)
  write(static_py, s)

  # First-run intro: the old bitmap was still visible even though the alt text
  # had already been rebranded. Render KO PILOT text instead.
  welcome = repo / "openpilot/selfdrive/carrot/web/src/features/intro/steps/welcome.js"
  s = read(welcome)
  old_logo = '<img class="intro-logo" src="${assetUrl("img_spinner_comma.png")}" alt="KOPilot" />'
  new_logo = '<div class="intro-logo intro-logo--kopilot" role="img" aria-label="KOPilot">KO PILOT</div>'
  if old_logo not in s:
    raise RuntimeError("intro legacy logo reference was not found")
  s = s.replace(old_logo, new_logo, 1)
  s = s.replace("const { t, ctx, assetUrl } = CarrotIntro;", "const { t, ctx } = CarrotIntro;")
  write(welcome, s)

  intro_css = repo / "openpilot/selfdrive/carrot/web/src/features/intro/style.css"
  css = read(intro_css)
  marker = ".intro-logo {\n"
  if ".intro-logo--kopilot" not in css:
    pos = css.find(marker)
    if pos < 0:
      raise RuntimeError("intro logo CSS marker not found")
    block = '''.intro-logo--kopilot {
  display: grid;
  place-items: center;
  min-height: clamp(88px, 16vw, 132px);
  font-size: clamp(32px, 6vw, 54px);
  font-weight: 900;
  letter-spacing: -0.055em;
  line-height: 1;
  color: var(--md-on-surface);
  text-shadow: 0 12px 42px color-mix(in srgb, var(--md-primary) 32%, transparent);
  white-space: nowrap;
}

'''
    css = css[:pos] + block + css[pos:]
  write(intro_css, css)

  intro_i18n = repo / "openpilot/selfdrive/carrot/web/src/features/intro/i18n.js"
  replace_text(intro_i18n, [
    ("당근서버", "KO 서버"),
    ("당근 서버", "KO 서버"),
    ("CarrotPilot", "KOPilot"),
  ])

  # Compatibility internals stay, but new/default names exposed to users are KO.
  config_py = repo / "openpilot/selfdrive/carrot/server/config.py"
  replace_text(config_py, [
    ('TMUX_WEB_SESSION = os.environ.get("CARROT_TMUX_WEB_SESSION", "carrot-terminal")',
     'TMUX_WEB_SESSION = os.environ.get("CARROT_TMUX_WEB_SESSION", "kopilot-terminal")'),
  ])

  writer_py = repo / "openpilot/selfdrive/carrot/server/services/youtube_live_writer.py"
  replace_text(writer_py, [("carrot-youtube-rtmp-writer", "kopilot-youtube-rtmp-writer")])
  live_py = repo / "openpilot/selfdrive/carrot/server/services/youtube_live.py"
  replace_text(live_py, [("carrot-youtube-live", "kopilot-youtube-live")])

  vision_cli = repo / "openpilot/selfdrive/carrot/server/terminal_commands/custom_commands/vision_test.py"
  replace_text(vision_cli, [
    ("주차 상태에서 당근 비전 카메라를 확인합니다.", "주차 상태에서 KO Vision 카메라를 확인합니다."),
  ])

  # Comments / source-facing wording in display modules; command/API identifiers
  # themselves are intentionally preserved.
  for rel in [
    "openpilot/selfdrive/carrot/web/js/realtime/raw_capnp.js",
    "openpilot/selfdrive/carrot/web/src/features/drive/contents/vision/ar/activation_gate.js",
    "openpilot/selfdrive/carrot/web/src/features/drive/contents/vision/hud/index.js",
  ]:
    p = repo / rel
    replace_text(p, [("당근비전", "KO Vision"), ("당근 아이콘", "KO 아이콘"), ("당근", "KO")])


def sweep_visible_branding(repo: Path) -> None:
  """Broad display-layer sweep without renaming compatibility identifiers."""
  roots = [
    repo / "openpilot/selfdrive/carrot/web",
    repo / "openpilot/selfdrive/ui",
    repo / "openpilot/selfdrive/carrot/server/features",
    repo / "openpilot/selfdrive/carrot/server/terminal_commands",
    repo / "openpilot/selfdrive/carrot/recovery",
  ]
  exts = {".py", ".js", ".mjs", ".html", ".css", ".json", ".md", ".txt", ".cc", ".cpp", ".h", ".hpp"}
  phrase_replacements = [
    ("CarrotPilot", "KOPilot"),
    ("Carrot Web", "KO Web"),
    ("Carrot Vision", "KO Vision"),
    ("Carrot Navi", "KO Navi"),
    ("Carrot Radar", "KO Radar"),
    ("Carrot Cruise", "KO Cruise"),
    ("Carrot Map", "KO Map"),
    ("Carrot map", "KO Map"),
    ("Carrot server", "KO server"),
    ("Carrot Server", "KO Server"),
    ("Carrot driving mode", "KO driving mode"),
    ("Carrot command", "KO command"),
    ("Carrot speed control", "KO speed control"),
    ("胡萝卜", "KO"),
    ("당근", "KO"),
  ]
  for root in roots:
    if not root.exists():
      continue
    for path in root.rglob("*"):
      if not path.is_file() or path.suffix.lower() not in exts:
        continue
      try:
        original = read(path)
      except UnicodeDecodeError:
        continue
      updated = original
      for old, new in phrase_replacements:
        updated = updated.replace(old, new)
      if updated != original:
        write(path, updated)


def patch_door_runtime(repo: Path) -> None:
  p = repo / "openpilot/selfdrive/carrot/server/features/ko_vehicle.py"
  s = read(p)

  if "import asyncio\n" not in s:
    s = s.replace("from __future__ import annotations\n\n", "from __future__ import annotations\n\nimport asyncio\n", 1)
  if "import time\n" not in s:
    s = s.replace("import os\n", "import os\nimport time\n", 1)

  if "DOOR_LOG_PATH" not in s:
    s = s.replace(
      'DOOR_CONFIG_PATH = Path(os.environ.get("KO_DOOR_CAN_CONFIG", "/data/ko/door_can.json"))\n',
      'DOOR_CONFIG_PATH = Path(os.environ.get("KO_DOOR_CAN_CONFIG", "/data/ko/door_can.json"))\n'
      'DOOR_LOG_PATH = Path(os.environ.get("KO_DOOR_CONTROL_LOG", "/data/ko/door_control.jsonl"))\n',
      1,
    )

  start = s.index("def _load_frames(action: str)")
  end = s.index("\n\nasync def status", start)
  loader = '''def _load_frames(action: str) -> list[tuple[int, bytes, int, int]]:
  if not DOOR_CONFIG_PATH.is_file():
    raise FileNotFoundError(str(DOOR_CONFIG_PATH))
  raw = json.loads(DOOR_CONFIG_PATH.read_text(encoding="utf-8"))
  items = raw.get(action)
  if not isinstance(items, list) or not items:
    raise ValueError(f"no verified '{action}' frames")
  out: list[tuple[int, bytes, int, int]] = []
  for i, frame in enumerate(items):
    if not isinstance(frame, dict):
      raise ValueError(f"frame {i} is not an object")
    a = frame.get("address")
    address = int(a, 0) if isinstance(a, str) else int(a)
    bus = int(frame.get("bus"))
    data = bytes.fromhex(str(frame.get("data", "")).replace(" ", ""))
    delay_ms = int(frame.get("delay_ms", 0) or 0)
    if not (0 <= address <= 0x1FFFFFFF):
      raise ValueError(f"frame {i} address out of range")
    if not (0 <= bus <= 3):
      raise ValueError(f"frame {i} bus out of range")
    if not (1 <= len(data) <= 64):
      raise ValueError(f"frame {i} payload length invalid")
    if not (0 <= delay_ms <= 1000):
      raise ValueError(f"frame {i} delay_ms out of range")
    out.append((address, data, bus, delay_ms))
  return out


def _door_log(payload: dict[str, Any]) -> None:
  try:
    DOOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DOOR_LOG_PATH.open("a", encoding="utf-8") as f:
      f.write(json.dumps({"ts": time.time(), **payload}, ensure_ascii=False, separators=(",", ":")) + "\\n")
  except Exception:
    pass
'''
  s = s[:start] + loader + s[end:]

  old_send = '''  try:
    sock = messaging.pub_sock("sendcan")
    sock.send(can_list_to_can_capnp(frames, msgtype="sendcan", valid=True))
  except Exception as exc:
    return web.json_response({"ok": False, "error": f"sendcan failed: {exc}"}, status=500)
  return web.json_response({"ok": True, "action": action, "frames": len(frames), "live": state})
'''
  new_send = '''  try:
    sock = messaging.pub_sock("sendcan")
    for address, data, bus, delay_ms in frames:
      if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)
      sock.send(can_list_to_can_capnp([(address, data, bus)], msgtype="sendcan", valid=True))
  except Exception as exc:
    _door_log({"ok": False, "action": action, "error": str(exc), "live": state})
    return web.json_response({"ok": False, "error": f"sendcan failed: {exc}"}, status=500)
  _door_log({"ok": True, "action": action, "frames": len(frames), "live": state})
  return web.json_response({"ok": True, "action": action, "frames": len(frames), "live": state})
'''
  if old_send not in s:
    raise RuntimeError("door send block did not match expected ko-rebrand base")
  s = s.replace(old_send, new_send, 1)
  write(p, s)

  # Ship the capture-derived candidate in the repo, but DO NOT activate it.
  shutil.copy2(DOOR_CANDIDATE, repo / "KO_DOOR_CANDIDATE.json")

  activate = repo / "scripts/kopilot_activate_door_candidate.sh"
  activate.parent.mkdir(parents=True, exist_ok=True)
  write(activate, '''#!/usr/bin/env bash
set -euo pipefail
cd /data/openpilot
mkdir -p /data/ko
cp -f KO_DOOR_CANDIDATE.json /data/ko/door_can.json
chmod 600 /data/ko/door_can.json
printf '%s\n' 'Candidate installed: /data/ko/door_can.json'
printf '%s\n' 'It is NOT enabled yet. In KO Web: Vehicle Aux -> KO 도어 제어 허용 = ON.'
printf '%s\n' 'Test only with ignition ON, P, standstill, valid CAN, and openpilot disengaged.'
''')
  activate.chmod(0o755)

  deactivate = repo / "scripts/kopilot_deactivate_door_candidate.sh"
  write(deactivate, '''#!/usr/bin/env bash
set -euo pipefail
rm -f /data/ko/door_can.json
cd /data/openpilot
python3 - <<'EOF'
from openpilot.common.params import Params
Params().put_bool("KoDoorControlEnabled", False)
print("KO door candidate removed and master toggle disabled")
EOF
''')
  deactivate.chmod(0o755)


def validate(repo: Path) -> None:
  # Python syntax for changed runtime modules.
  for rel in [
    "openpilot/selfdrive/carrot/server/services/youtube_live_muxer.py",
    "openpilot/selfdrive/carrot/server/services/youtube_live_writer.py",
    "openpilot/selfdrive/carrot/server/services/youtube_live.py",
    "openpilot/system/ui/spinner.py",
    "openpilot/selfdrive/carrot/server/features/ko_vehicle.py",
    "openpilot/selfdrive/carrot/server/features/static.py",
  ]:
    py_compile.compile(str(repo / rel), doraise=True)

  # Candidate JSON must parse, and must remain opt-in rather than being copied
  # into /data by the patcher itself.
  candidate = json.loads(read(repo / "KO_DOOR_CANDIDATE.json"))
  assert len(candidate.get("lock", [])) == 3
  assert len(candidate.get("unlock", [])) == 4
  assert all(int(x["bus"]) == 0 for x in candidate["lock"] + candidate["unlock"])

  # No old branded bitmap may be referenced by the two actual loading/intro UIs.
  spinner = read(repo / "openpilot/system/ui/spinner.py")
  welcome = read(repo / "openpilot/selfdrive/carrot/web/src/features/intro/steps/welcome.js")
  if "img_spinner_comma.png" in spinner or "img_spinner_comma.png" in welcome:
    raise RuntimeError("legacy loading/intro bitmap is still referenced")
  if "KO PILOT" not in spinner or "KO PILOT" not in welcome:
    raise RuntimeError("KO PILOT mark missing")

  # Display-facing files should no longer contain the known visible legacy words.
  checks = {
    "openpilot/selfdrive/carrot/web/js/translations/ko.js": ["당근"],
    "openpilot/selfdrive/carrot/web/js/translations/en.js": ["Carrot server", "Carrot map", 'carrot: "Carrot"', "Carrot Info"],
    "openpilot/selfdrive/carrot/web/js/translations/zh.js": ["Carrot 服务器", "Carrot 导航", "Carrot 地图", "胡萝卜"],
    "openpilot/selfdrive/carrot/web/src/features/intro/i18n.js": ["당근서버", "당근 서버"],
    "openpilot/selfdrive/carrot/server/features/static.py": ["<title>Carrot Web</title>", "Carrot Web 업데이트 적용 중"],
  }
  for rel, tokens in checks.items():
    content = read(repo / rel)
    bad = [t for t in tokens if t in content]
    if bad:
      raise RuntimeError(f"visible legacy branding remains in {rel}: {bad}")

  if "tmux carrot-terminal" in read(repo / "openpilot/selfdrive/carrot/web/js/translations/ko.js"):
    raise RuntimeError("old terminal session display remains")


def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--repo", required=True)
  ap.add_argument("--push", action="store_true")
  ap.add_argument("--local", action="store_true", help="build locally without fetching/pushing")
  args = ap.parse_args()

  repo = Path(args.repo).resolve()
  if not (repo / ".git").exists():
    raise SystemExit(f"Not a Git repository: {repo}")

  # Remove only the known temporary download artifacts from the earlier phone
  # log-transfer workflow. Unknown local edits are never discarded silently.
  for rel in [
    "openpilot/selfdrive/carrot/web/door_logs.tgz",
    "openpilot/selfdrive/carrot/web/door_download.html",
  ]:
    temp = repo / rel
    if temp.exists():
      temp.unlink()

  status = output(["git", "status", "--porcelain"], repo)
  if status:
    raise SystemExit("Working tree is not clean. Refusing to discard unknown edits:\n" + status)

  if not args.local:
    # GitHub-side promotion must be completed first: ko-wip must point at the
    # verified rebrand commit before the test branch is created/pushed.
    run(["git", "fetch", "origin", BASE_BRANCH], repo)
    run(["git", "switch", "-C", BASE_BRANCH, f"origin/{BASE_BRANCH}"], repo)
    base = output(["git", "rev-parse", "HEAD"], repo)
    if base != BASE_SHA:
      raise SystemExit(
        f"Unexpected origin/{BASE_BRANCH} HEAD: {base}\n"
        f"Expected verified stable SHA: {BASE_SHA}\n"
        "Promote ko-rebrand to ko-wip first, then rerun."
      )
  else:
    # Phone/local workflow: the device is currently on the verified ko-rebrand
    # commit. Pin a local ko-wip branch to that exact verified SHA, then branch
    # test from it. This does not modify GitHub by itself.
    current = output(["git", "rev-parse", "HEAD"], repo)
    if current != BASE_SHA:
      raise SystemExit(f"Unexpected current HEAD: {current}\nExpected verified stable SHA: {BASE_SHA}")
    run(["git", "branch", "-f", BASE_BRANCH, BASE_SHA], repo)
    run(["git", "switch", BASE_BRANCH], repo)
    base = output(["git", "rev-parse", "HEAD"], repo)

  run(["git", "switch", "-C", NEW_BRANCH, BASE_BRANCH], repo)

  # Reuse the already prepared and syntax-checked v1.5 audio + spinner patch.
  v15.patch_muxer(repo)
  v15.patch_writer(repo)
  v15.patch_live_service(repo)
  v15.patch_spinner(repo)

  patch_branding(repo)
  sweep_visible_branding(repo)
  patch_door_runtime(repo)

  manifest = {
    "version": "v1.6-test",
    "branch": NEW_BRANCH,
    "base_branch": BASE_BRANCH,
    "base_sha": BASE_SHA,
    "features": [
      "YouTube Live real cabin microphone audio: rawAudioData 16 kHz mono -> 44.1 kHz stereo AAC",
      "video-priority RTMP queue with silence fallback for dropped/missing audio",
      "KO PILOT build/loading spinner and first-run intro mark",
      "remaining user-visible Korean/English/Chinese Carrot branding swept to KOPilot/KO",
      "kopilot-terminal default display/session name (legacy env key retained)",
      "capture-derived 0x3FF door lock/unlock replay candidate, disabled by default",
      "door sequence delay_ms support and /data/ko/door_control.jsonl attempt logging",
    ],
    "compatibility_preserved": [
      "openpilot/selfdrive/carrot package path",
      "Carrot* Params and existing API route identifiers",
      "/data/carrot persisted-state path and legacy env keys",
      "external carrotpilot.app service URLs where still required",
      "legacy __CARROT_BOOTSTRAP__ alias (new __KOPILOT_BOOTSTRAP__ is authoritative)",
    ],
  }
  write(repo / "KO_INTEGRATED_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

  validate(repo)
  run(["git", "add", "-A"], repo)
  changed = output(["git", "status", "--short"], repo)
  if not changed:
    raise SystemExit("No changes produced")
  print("\n=== CHANGES ===\n" + changed)
  run(["git", "commit", "-m", "KOPilot integrated audio branding and door candidate"], repo)

  print("\n=== VERIFY ===")
  print("BRANCH =", output(["git", "branch", "--show-current"], repo))
  print("HEAD   =", output(["git", "rev-parse", "--short", "HEAD"], repo))
  print("EXEC   =", len([x for x in output(["git", "ls-files", "-s"], repo).splitlines() if x.startswith("100755 ")]))
  print("SYMLINK=", len([x for x in output(["git", "ls-files", "-s"], repo).splitlines() if x.startswith("120000 ")]))
  print("DOOR   = candidate included, NOT activated")

  if args.push:
    run(["git", "push", "-u", "origin", NEW_BRANCH], repo)
    print("\nPUSH_OK")
  else:
    print("\nLOCAL_BUILD_OK")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
