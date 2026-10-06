#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import py_compile
import subprocess
from pathlib import Path

BASE_BRANCH = "ko-rebrand"
BASE_SHA = "e4d48a534f53fb5c32ac477780d9846a856c3e3b"
NEW_BRANCH = "ko-monitor-audio"

def run(cmd, cwd: Path, *, check=True, capture=False):
  print("+", " ".join(str(x) for x in cmd))
  return subprocess.run(
    [str(x) for x in cmd],
    cwd=str(cwd),
    check=check,
    text=True,
    stdout=subprocess.PIPE if capture else None,
    stderr=subprocess.STDOUT if capture else None,
  )

def output(cmd, cwd: Path) -> str:
  return run(cmd, cwd, capture=True).stdout.strip()

def read(path: Path) -> str:
  return path.read_text(encoding="utf-8")

def write(path: Path, data: str) -> None:
  with path.open("w", encoding="utf-8", newline="\n") as f:
    f.write(data)

def replace_once(text: str, old: str, new: str, label: str) -> str:
  count = text.count(old)
  if count != 1:
    raise RuntimeError(f"{label}: expected exactly 1 match, found {count}")
  return text.replace(old, new, 1)

def patch_muxer(repo: Path) -> None:
  p = repo / "openpilot/selfdrive/carrot/server/services/youtube_live_muxer.py"
  s = read(p)

  s = replace_once(
    s,
    '''    self._audio_codec.time_base = self._audio_time_base
    self._audio_codec.open()
    self._packet_index = 0
''',
    '''    self._audio_codec.time_base = self._audio_time_base
    self._audio_codec.open()
    self._audio_resampler = av.AudioResampler(format="fltp", layout="stereo", rate=AUDIO_RATE)
    self._input_audio_pts = 0
    self._packet_index = 0
''',
    "muxer audio resampler init",
  )

  s = replace_once(
    s,
    '''  def close(self) -> None:
''',
    '''  def mux_audio(self, pcm_s16_mono: bytes, *, sample_rate: int = 16_000) -> None:
    # Mux signed 16-bit mono microphone PCM into the YouTube AAC track.
    with self._lock:
      if self._closed:
        raise RuntimeError("FLV muxer is closed")
      if not pcm_s16_mono:
        return

      rate = max(8_000, int(sample_rate or 16_000))
      samples = len(pcm_s16_mono) // 2
      if samples <= 0:
        return

      frame = self._av.AudioFrame(format="s16", layout="mono", samples=samples)
      frame.sample_rate = rate
      frame.time_base = Fraction(1, rate)
      frame.pts = self._input_audio_pts
      self._input_audio_pts += samples
      frame.planes[0].update(pcm_s16_mono[:samples * 2])

      # micd publishes 16 kHz mono. YouTube receives 44.1 kHz stereo AAC.
      for out_frame in self._audio_resampler.resample(frame):
        out_frame.pts = self._audio_pts
        out_frame.time_base = self._audio_time_base
        out_frame.sample_rate = AUDIO_RATE
        for packet in self._audio_codec.encode(out_frame):
          self._write_audio_packet(packet)
        self._audio_pts += int(out_frame.samples)

  def close(self) -> None:
''',
    "muxer live audio method",
  )

  s = replace_once(
    s,
    '''      # keep the silent audio track filled up to the current video time so a
      # dropped-frame gap stays A/V aligned
      self._mux_silence_until(int(video_ms * AUDIO_RATE / 1000))
''',
    '''      # If live microphone audio is late or absent, fill only the missing
      # span with silence so the FLV timeline remains valid and A/V stays aligned.
      self._mux_silence_until(int(video_ms * AUDIO_RATE / 1000))
''',
    "muxer silence fallback comment",
  )
  write(p, s)

def patch_writer(repo: Path) -> None:
  p = repo / "openpilot/selfdrive/carrot/server/services/youtube_live_writer.py"
  s = read(p)

  s = replace_once(
    s,
    '''    self._queue: asyncio.Queue[tuple[bytes, bool] | None] = asyncio.Queue(maxsize=max(1, int(max_frames)))
''',
    '''    self._queue: asyncio.Queue[tuple[str, bytes, int] | None] = asyncio.Queue(maxsize=max(1, int(max_frames)))
''',
    "writer queue type",
  )

  s = replace_once(
    s,
    '''  def enqueue(self, payload: bytes, *, keyframe: bool) -> bool:
    task = self._task
    self._last_rejection = ""
    if self._error:
      self._last_rejection = self._error
      return False
    if task is None or task.done():
      self._last_rejection = "RTMP writer is not running"
      return False
    frame = bytes(payload)
    next_pending_bytes = self._pending_bytes + len(frame)
    if next_pending_bytes > self._max_bytes:
      self._last_rejection = f"RTMP frame backlog reached {self._max_bytes} bytes"
      return False
    try:
      self._queue.put_nowait((frame, bool(keyframe)))
    except asyncio.QueueFull:
      self._last_rejection = f"RTMP frame backlog reached {self.capacity} frames"
      return False
    self._pending_bytes = next_pending_bytes
    self._high_watermark = max(self._high_watermark, self.pending_frames)
    self._high_watermark_bytes = max(self._high_watermark_bytes, self._pending_bytes)
    return True
''',
    '''  def _enqueue_item(self, kind: str, payload: bytes, value: int) -> bool:
    task = self._task
    self._last_rejection = ""
    if self._error:
      self._last_rejection = self._error
      return False
    if task is None or task.done():
      self._last_rejection = "RTMP writer is not running"
      return False
    data = bytes(payload)
    next_pending_bytes = self._pending_bytes + len(data)
    if next_pending_bytes > self._max_bytes:
      self._last_rejection = f"RTMP frame backlog reached {self._max_bytes} bytes"
      return False
    try:
      self._queue.put_nowait((kind, data, int(value)))
    except asyncio.QueueFull:
      self._last_rejection = f"RTMP frame backlog reached {self.capacity} frames"
      return False
    self._pending_bytes = next_pending_bytes
    self._high_watermark = max(self._high_watermark, self.pending_frames)
    self._high_watermark_bytes = max(self._high_watermark_bytes, self._pending_bytes)
    return True

  def enqueue(self, payload: bytes, *, keyframe: bool) -> bool:
    return self._enqueue_item("video", payload, int(bool(keyframe)))

  def enqueue_audio(self, pcm_s16_mono: bytes, *, sample_rate: int) -> bool:
    return self._enqueue_item("audio", pcm_s16_mono, max(8_000, int(sample_rate or 16_000)))
''',
    "writer enqueue methods",
  )

  s = replace_once(
    s,
    '''      if item is not None:
        discarded += 1
        self._pending_bytes = max(0, self._pending_bytes - len(item[0]))
''',
    '''      if item is not None:
        kind, payload, _value = item
        if kind == "video":
          discarded += 1
        self._pending_bytes = max(0, self._pending_bytes - len(payload))
''',
    "writer stop queue drain",
  )

  s = replace_once(
    s,
    '''        payload, keyframe = item
        self._pending_bytes = max(0, self._pending_bytes - len(payload))
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
          self._executor,
          partial(self._muxer.mux, payload, keyframe=keyframe),
        )
        elapsed_ms = max(0, int((time.monotonic() - started) * 1_000))
        self._last_write_ms = elapsed_ms
        self._max_write_ms = max(self._max_write_ms, elapsed_ms)
        self._frames_written += 1
''',
    '''        kind, payload, value = item
        self._pending_bytes = max(0, self._pending_bytes - len(payload))
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        if kind == "audio":
          await loop.run_in_executor(
            self._executor,
            partial(self._muxer.mux_audio, payload, sample_rate=value),
          )
        else:
          await loop.run_in_executor(
            self._executor,
            partial(self._muxer.mux, payload, keyframe=bool(value)),
          )
        elapsed_ms = max(0, int((time.monotonic() - started) * 1_000))
        self._last_write_ms = elapsed_ms
        self._max_write_ms = max(self._max_write_ms, elapsed_ms)
        if kind == "video":
          self._frames_written += 1
''',
    "writer dispatch",
  )
  write(p, s)

def patch_live_service(repo: Path) -> None:
  p = repo / "openpilot/selfdrive/carrot/server/services/youtube_live.py"
  s = read(p)

  s = replace_once(
    s,
    '''    self._messaging: Any | None = None
    self._socket: Any | None = None
''',
    '''    self._messaging: Any | None = None
    self._socket: Any | None = None
    self._audio_socket: Any | None = None
''',
    "live audio socket init",
  )

  s = replace_once(
    s,
    '''  def _recv_frame(self) -> tuple[bytes, bytes, int | None, bool, int, int]:
''',
    '''  def _get_audio_socket(self) -> Any | None:
    if self._audio_socket is not None:
      return self._audio_socket
    messaging = self._get_messaging()
    if messaging is None:
      return None
    try:
      self._audio_socket = messaging.sub_sock("rawAudioData", conflate=False)
    except Exception:
      self._audio_socket = None
    return self._audio_socket

  def _drain_audio(self, writer: RtmpFrameWriter) -> None:
    # Video continuity wins over audio when RTMP is congested. micd publishes
    # one 50 ms mono int16 block at 16 kHz, so normally this drains 0-1 blocks.
    if writer.pending_frames >= max(1, writer.capacity // 2):
      return
    messaging = self._get_messaging()
    sock = self._get_audio_socket()
    if messaging is None or sock is None:
      return

    for _ in range(8):
      if writer.pending_frames >= max(1, writer.capacity // 2):
        break
      try:
        msg = messaging.recv_one_or_none(sock)
      except Exception:
        break
      if msg is None:
        break
      audio = getattr(msg, "rawAudioData", None)
      if audio is None:
        continue
      pcm = bytes(getattr(audio, "data", b"") or b"")
      sample_rate = int(getattr(audio, "sampleRate", 0) or 16_000)
      if pcm and not writer.enqueue_audio(pcm, sample_rate=sample_rate):
        # Audio is best-effort. Missing spans are replaced with silence.
        break

  def _recv_frame(self) -> tuple[bytes, bytes, int | None, bool, int, int]:
''',
    "live audio methods",
  )

  s = replace_once(
    s,
    '''    data = self._caption_injector.inject(
      data,
      enabled=self._param_bool(YOUTUBE_TIMESTAMP_PARAM),
    )
''',
    '''    writer = self._writer
    if writer is not None:
      self._drain_audio(writer)

    data = self._caption_injector.inject(
      data,
      enabled=self._param_bool(YOUTUBE_TIMESTAMP_PARAM),
    )
''',
    "live audio drain call",
  )

  s = replace_once(
    s,
    '''  async def _stop_stream(self) -> None:
    transport = self._transport
''',
    '''  async def _stop_stream(self) -> None:
    audio_socket = self._audio_socket
    self._audio_socket = None
    if audio_socket is not None:
      try:
        audio_socket.close()
      except Exception:
        pass

    transport = self._transport
''',
    "live audio socket close",
  )
  write(p, s)

def patch_spinner(repo: Path) -> None:
  p = repo / "openpilot/system/ui/spinner.py"
  s = read(p)

  s = replace_once(
    s,
    '''  STATUS_LINE_MARGIN = 14
else:
''',
    '''  STATUS_LINE_MARGIN = 14
  LOGO_FONT_SIZE = 58
else:
''',
    "spinner big logo font",
  )

  s = replace_once(
    s,
    '''  STATUS_LINE_MARGIN = 4
DEGREES_PER_SECOND = 360.0  # one full rotation per second
''',
    '''  STATUS_LINE_MARGIN = 4
  LOGO_FONT_SIZE = 24
DEGREES_PER_SECOND = 360.0  # one full rotation per second
''',
    "spinner small logo font",
  )

  s = replace_once(
    s,
    '''    self._comma_texture = gui_app.texture("img_spinner_comma.png", TEXTURE_SIZE, TEXTURE_SIZE)
    self._spinner_texture = gui_app.texture("img_spinner_track.png", TEXTURE_SIZE, TEXTURE_SIZE, alpha_premultiply=True)
''',
    '''    self._spinner_texture = gui_app.texture("img_spinner_track.png", TEXTURE_SIZE, TEXTURE_SIZE, alpha_premultiply=True)
''',
    "spinner remove branded center image",
  )

  s = replace_once(
    s,
    '''    comma_position = rl.Vector2(center.x - TEXTURE_SIZE / 2.0, center.y - TEXTURE_SIZE / 2.0)

    delta_time = rl.get_frame_time()
''',
    '''    delta_time = rl.get_frame_time()
''',
    "spinner remove old center position",
  )

  s = replace_once(
    s,
    '''    # Draw rotating spinner and static comma logo
    rl.draw_texture_pro(self._spinner_texture, rl.Rectangle(0, 0, TEXTURE_SIZE, TEXTURE_SIZE),
                        rl.Rectangle(center.x, center.y, TEXTURE_SIZE, TEXTURE_SIZE),
                        spinner_origin, self._rotation, rl.WHITE)
    rl.draw_texture_v(self._comma_texture, comma_position, rl.WHITE)

    # Display the progress bar or text based on user input
''',
    '''    # Draw the generic rotating track with a KO PILOT center mark.
    rl.draw_texture_pro(self._spinner_texture, rl.Rectangle(0, 0, TEXTURE_SIZE, TEXTURE_SIZE),
                        rl.Rectangle(center.x, center.y, TEXTURE_SIZE, TEXTURE_SIZE),
                        spinner_origin, self._rotation, rl.WHITE)

    logo_text = "KO PILOT"
    logo_font = gui_app.font(FontWeight.PRETENDARD)
    logo_font_size = LOGO_FONT_SIZE * FONT_SCALE
    logo_size = rl.measure_text_ex(logo_font, logo_text, logo_font_size, 0.0)
    draw_text_ex = getattr(rl, "_orig_draw_text_ex", rl.draw_text_ex)
    draw_text_ex(
      logo_font,
      logo_text,
      rl.Vector2(round(center.x - logo_size.x / 2.0), round(center.y - logo_size.y / 2.0)),
      logo_font_size,
      0.0,
      rl.WHITE,
    )

    # Display the progress bar or text based on user input
''',
    "spinner KO PILOT center",
  )
  write(p, s)

def main() -> int:
  ap = argparse.ArgumentParser()
  ap.add_argument("--repo", required=True)
  ap.add_argument("--push", action="store_true")
  args = ap.parse_args()

  repo = Path(args.repo).resolve()
  if not (repo / ".git").exists():
    raise SystemExit(f"Not a Git repository: {repo}")
  if output(["git", "status", "--porcelain"], repo):
    raise SystemExit("Working tree is not clean. Commit/stash local edits first.")

  run(["git", "fetch", "origin", BASE_BRANCH], repo)
  run(["git", "switch", "-C", BASE_BRANCH, f"origin/{BASE_BRANCH}"], repo)
  base = output(["git", "rev-parse", "HEAD"], repo)
  if base != BASE_SHA:
    raise SystemExit(f"Unexpected {BASE_BRANCH} HEAD: {base}\nExpected: {BASE_SHA}")

  run(["git", "switch", "-C", NEW_BRANCH, BASE_BRANCH], repo)

  patch_muxer(repo)
  patch_writer(repo)
  patch_live_service(repo)
  patch_spinner(repo)

  manifest = {
    "version": "v1.5",
    "branch": NEW_BRANCH,
    "base_branch": BASE_BRANCH,
    "base_sha": BASE_SHA,
    "changes": [
      "YouTube Live: micd rawAudioData 16 kHz mono PCM -> 44.1 kHz stereo AAC",
      "YouTube Live: video queue has priority; congested audio is dropped and silence-filled",
      "Loading/build spinner: old branded center bitmap disabled",
      "Loading/build spinner: KO PILOT text rendered in center",
    ],
    "rollback": ["ko-rebrand untouched", "ko-wip untouched"],
  }
  write(repo / "KO_MONITOR_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

  for rel in [
    "openpilot/selfdrive/carrot/server/services/youtube_live_muxer.py",
    "openpilot/selfdrive/carrot/server/services/youtube_live_writer.py",
    "openpilot/selfdrive/carrot/server/services/youtube_live.py",
    "openpilot/system/ui/spinner.py",
  ]:
    py_compile.compile(str(repo / rel), doraise=True)

  run(["git", "add", "-A"], repo)
  changed = output(["git", "status", "--short"], repo)
  if not changed:
    raise SystemExit("No changes produced.")
  print("\n=== CHANGES ===\n" + changed)

  run(["git", "commit", "-m", "KOPilot pet monitor live audio and KO PILOT boot mark"], repo)
  print("\n=== VERIFY ===")
  print("BRANCH =", output(["git", "branch", "--show-current"], repo))
  print("HEAD   =", output(["git", "rev-parse", "--short", "HEAD"], repo))
  print("EXEC   =", len([x for x in output(["git", "ls-files", "-s"], repo).splitlines() if x.startswith("100755 ")]))
  print("SYMLINK=", len([x for x in output(["git", "ls-files", "-s"], repo).splitlines() if x.startswith("120000 ")]))

  if args.push:
    run(["git", "fetch", "origin", NEW_BRANCH], repo, check=False)
    run(["git", "push", "--force-with-lease", "-u", "origin", NEW_BRANCH], repo)
    print("\nPUSH_OK")
  return 0

if __name__ == "__main__":
  raise SystemExit(main())
