const POLL_MS = 1000;

function fmtBytes(value) {
  const n = Number(value) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function fmtTime(value) {
  const seconds = Math.max(0, Math.round(Number(value) || 0));
  const minutes = Math.floor(seconds / 60);
  const remain = seconds % 60;
  return `${minutes}분 ${String(remain).padStart(2, "0")}초`;
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
  });
  let payload = {};
  try { payload = await response.json(); } catch {}
  if (!response.ok || payload?.ok === false) {
    throw new Error(payload?.error || `HTTP ${response.status}`);
  }
  return payload;
}

function build(root) {
  const d = root.ownerDocument;
  const section = d.createElement("section");
  section.className = "setting-section-block ui-stagger-item";
  section.dataset.settingsExtensionPanel = "ko-drive-log";

  const card = d.createElement("div");
  card.className = "setting-group-card";
  card.innerHTML = `
    <div class="setting-profile-card__rows">
      <div class="setting-profile-row setting-profile-row--name">
        <div>
          <strong class="setting-profile-row__label">KO 주행/튜닝 로그</strong>
          <p class="muted mt-sm">PID·커브 안정성·앞차 감지 분석용 데이터를 기록합니다. 영상/GPS 좌표는 저장하지 않습니다.</p>
        </div>
        <span class="chip chip--compact" data-role="state">OFF</span>
      </div>
      <div class="setting-profile-row">
        <div>
          <strong>로그 기록</strong>
          <p class="muted mt-sm" data-role="detail">대기 중</p>
        </div>
        <label style="display:flex;align-items:center;gap:10px;font-weight:700;">
          <input data-role="toggle" type="checkbox" style="width:30px;height:30px;accent-color:currentColor;">
          <span>ON/OFF</span>
        </label>
      </div>
    </div>
    <div class="ui-action-grid">
      <button type="button" class="smallBtn" data-role="mark">문제 시점 표시</button>
      <button type="button" class="smallBtn" data-role="download">스마트폰에 로그 저장</button>
    </div>`;

  section.appendChild(card);
  root.appendChild(section);
  return {section, card};
}

export function mountKoDriveLogPanel({root, lifecycle}) {
  const view = build(root);
  const state = view.card.querySelector('[data-role="state"]');
  const detail = view.card.querySelector('[data-role="detail"]');
  const toggle = view.card.querySelector('[data-role="toggle"]');
  const mark = view.card.querySelector('[data-role="mark"]');
  const download = view.card.querySelector('[data-role="download"]');

  let current = null;
  let busy = false;

  function render() {
    const enabled = Boolean(current?.enabled);
    toggle.checked = enabled;
    toggle.disabled = busy;

    state.textContent = busy ? "처리 중…" : enabled ? "REC" : "OFF";
    state.className = "chip chip--compact" + (enabled ? " chip--warning" : "");

    if (current) {
      const parts = [];
      if (enabled) parts.push(fmtTime(current.elapsed_s));
      if (current.filename) parts.push(current.filename);
      parts.push(`${current.samples || 0} samples`);
      parts.push(fmtBytes(current.bytes));
      if (current.last_error) parts.push(`오류: ${current.last_error}`);
      detail.textContent = parts.join(" · ");
    } else {
      detail.textContent = "상태 확인 중";
    }

    mark.disabled = busy || !enabled;
    download.disabled = busy || !current?.filename;
  }

  async function refresh() {
    if (busy || lifecycle.destroyed || document.hidden) return;
    try {
      current = await requestJson("/api/ko/drive_log/status");
      render();
    } catch (error) {
      current = null;
      detail.textContent = `상태 오류: ${error.message}`;
      render();
    }
  }

  async function setEnabled(enabled) {
    if (busy) return;
    busy = true;
    render();
    try {
      current = await requestJson("/api/ko/drive_log/enable", {
        method: "POST",
        body: JSON.stringify({enabled}),
      });
      globalThis.showAppToast?.(
        enabled ? "KO 주행 로그 기록을 시작했습니다." : "KO 주행 로그 기록을 종료했습니다."
      );
    } catch (error) {
      globalThis.showAppToast?.(`로그 전환 실패: ${error.message}`, {tone:"error"});
    } finally {
      busy = false;
      await refresh();
    }
  }

  async function markEvent() {
    if (busy || !current?.enabled) return;
    try {
      await requestJson("/api/ko/drive_log/mark", {
        method: "POST",
        body: JSON.stringify({label:"user_event"}),
      });
      globalThis.showAppToast?.("현재 시점을 로그에 표시했습니다.");
    } catch (error) {
      globalThis.showAppToast?.(`표시 실패: ${error.message}`, {tone:"error"});
    }
  }

  function saveLog() {
    const a = document.createElement("a");
    a.href = `/api/ko/drive_log/download?t=${Date.now()}`;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  const onToggle = () => void setEnabled(toggle.checked);
  const onMark = () => void markEvent();
  const onDownload = () => saveLog();

  toggle.addEventListener("change", onToggle);
  mark.addEventListener("click", onMark);
  download.addEventListener("click", onDownload);

  lifecycle.addCleanup(() => toggle.removeEventListener("change", onToggle));
  lifecycle.addCleanup(() => mark.removeEventListener("click", onMark));
  lifecycle.addCleanup(() => download.removeEventListener("click", onDownload));

  lifecycle.setIntervalWhileMounted(refresh, POLL_MS);
  render();
  void refresh();

  return {
    root: view.section,
    sync() { render(); },
    destroy() { view.section.remove(); },
  };
}
