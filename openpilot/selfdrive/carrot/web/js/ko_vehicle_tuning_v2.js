/* KO Tune v2 standalone plugin. No generated bundle rebuild required. */
(function () {
  "use strict";

// KO auto-tuning analysis: records driver-selected axes and analyzes existing logs.
// The controller gains are never changed by this extension.
function mountKoTuneAnalysis(card, lifecycle) {
  const container = card.ownerDocument.createElement('div');
  container.className = 'setting-profile-card__rows';
  container.style.marginTop = '14px';
  container.innerHTML = `
    <div class="setting-profile-row"><div><strong>조향·감속 자동튜닝 (분석 모드)</strong>
      <p class="muted mt-sm">기존 10Hz 주행 로그에서 응답 오차를 자동 분석합니다. 제어 계수는 바꾸지 않습니다.</p></div></div>
    <div class="setting-profile-row"><div>조향 응답 분석</div><label><input type="checkbox" data-role="ko-steer-tune"> ON</label></div>
    <div class="setting-profile-row"><div>감속 응답 분석</div><label><input type="checkbox" data-role="ko-decel-tune"> ON</label></div>
    <div class="ui-action-grid"><button type="button" class="smallBtn" data-role="ko-tune-run">주행 로그 자동 분석</button></div>
    <p class="muted mt-sm" data-role="ko-tune-output" style="white-space:pre-wrap">설정값을 불러오는 중…</p>`;
  card.appendChild(container);
  const steer = container.querySelector('[data-role="ko-steer-tune"]');
  const decel = container.querySelector('[data-role="ko-decel-tune"]');
  const run = container.querySelector('[data-role="ko-tune-run"]');
  const output = container.querySelector('[data-role="ko-tune-output"]');
  let busy = false;
  async function query(url, body) {
    const response = await fetch(url, {method: body ? 'POST' : 'GET', cache: 'no-store',
      headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }
  async function load() {
    try {
      const data = await query('/api/ko/auto_tune/status');
      steer.checked = Boolean(data.config?.steering);
      decel.checked = Boolean(data.config?.deceleration);
      output.textContent = '설정 준비됨 · 로그 기록 ON 후 주행하여 분석하세요. 실시간 게인 변경 없음.';
    } catch (error) { output.textContent = `자동튜닝 상태 확인 실패: ${error.message}`; }
  }
  async function update(key, value) {
    if (busy) return;
    busy = true;
    try {
      await query('/api/ko/auto_tune/config', {[key]: value});
      output.textContent = `${key === 'steering' ? '조향' : '감속'} 분석 ${value ? 'ON' : 'OFF'} · 제어 계수 변경 없음`;
    } catch (error) {
      output.textContent = `설정 실패: ${error.message}`;
      if (key === 'steering') steer.checked = !value;
      else decel.checked = !value;
    } finally {busy = false;}
  }
  async function analyze() {
    if (busy) return;
    busy = true;
    run.disabled = true;
    output.textContent = '기존 로그 분석 중…';
    try {
      const data = await query('/api/ko/auto_tune/analyze', {});
      const format = (label, axis) => {
        if (axis?.state === 'disabled') return `${label}: OFF`;
        if (axis?.state !== 'analyzed') return `${label}: 데이터 부족 (${axis?.samples || 0}/${axis?.required_samples || '-'} 샘플)`;
        return `${label}: RMS ${axis.rms_error} ${axis.unit}, 평균오차 ${axis.mean_error} ${axis.unit} (${axis.samples}개)`;
      };
      output.textContent = `${format('조향', data.steering)}\n${format('감속', data.deceleration)}\n분석만 수행 · 주행 제어/게인 변경 없음`;
    } catch (error) {output.textContent = `분석 실패: ${error.message}`;}
    finally {busy = false; run.disabled = false;}
  }
  const onS = () => void update('steering', steer.checked);
  const onD = () => void update('deceleration', decel.checked);
  const onR = () => void analyze();
  steer.addEventListener('change', onS);
  decel.addEventListener('change', onD);
  run.addEventListener('click', onR);
  lifecycle.addCleanup(() => {
    steer.removeEventListener('change', onS);
    decel.removeEventListener('change', onD);
    run.removeEventListener('click', onR);
  });
  void load();
}

  const registry = window.CarrotSettingsExtensions;
  if (!registry || typeof registry.register !== 'function') return;
  registry.register({
    id: 'ko-auto-tuning-v2',
    matches(context) {return context.group === 'VEH_AUX' && !context.detailMode && Boolean(context.root);},
    mount(context) {
      const section = context.root.ownerDocument.createElement('section');
      section.className = 'setting-section-block ui-stagger-item';
      section.dataset.settingsExtensionPanel = 'ko-auto-tuning-v2';
      const card = context.root.ownerDocument.createElement('div');
      card.className = 'setting-group-card';
      section.appendChild(card);
      context.root.appendChild(section);
      mountKoTuneAnalysis(card, context.lifecycle);
      return {root: section, sync() {}, destroy() {section.remove();}};
    }
  });
  // The old panel assumes TX return = physical door actuation. Suppress that UI.
  const stylesheet = document.createElement('style');
  stylesheet.textContent = '[data-settings-extension-panel="ko-door-control"]{display:none!important}';
  document.head.appendChild(stylesheet);
  registry.register({
    id: 'ko-door-status-safe-v2',
    matches(context) {return context.group === 'VEH_AUX' && !context.detailMode && Boolean(context.root);},
    mount(context) {
      const doc = context.root.ownerDocument;
      const section = doc.createElement('section');
      section.className = 'setting-section-block ui-stagger-item';
      section.dataset.settingsExtensionPanel = 'ko-door-status-safe-v2';
      const card = doc.createElement('div');
      card.className = 'setting-group-card';
      card.innerHTML = `<div class="setting-profile-row"><div><strong>도어 잠금 상태 / 제어</strong>
        <p class="muted mt-sm">물리 도어 상태는 0x411/0x414로 조회합니다. 잠금·해제 CAN 구동 프로토콜은 미검증이며 송신을 차단합니다.</p></div></div>
        <p class="muted mt-sm" data-role="ko-door-state">도어 상태 조회 필요</p>
        <div class="ui-action-grid"><button type="button" class="smallBtn" data-role="ko-door-refresh">잠금 상태 확인</button>
        <button type="button" class="smallBtn" disabled>문 잠금 (미지원)</button>
        <button type="button" class="smallBtn" disabled>잠금 해제 (미지원)</button></div>`;
      section.appendChild(card);
      context.root.appendChild(section);
      const value=card.querySelector('[data-role="ko-door-state"]');
      const check=card.querySelector('[data-role="ko-door-refresh"]');
      async function refresh() {
        check.disabled = true;
        value.textContent = '현재 잠금 상태 확인 중…';
        try {
          const response=await fetch('/api/ko/door/state',{cache:'no-store'});
          const data=await response.json();
          if (!response.ok||!data.ok) throw new Error(data.error||`HTTP ${response.status}`);
          const state = data.door_state?.state || 'unknown';
          value.textContent = `CAN 잠금 상태(추정): ${state==='locked'?'잠금':state==='unlocked'?'해제':'불확실'} · 실제 구동 명령은 비활성`;
        } catch(error){value.textContent=`조회 실패: ${error.message}`;}
        finally{check.disabled=false;}
      }
      const onClick=()=>void refresh();
      check.addEventListener('click',onClick);
      context.lifecycle.addCleanup(()=>check.removeEventListener('click',onClick));
      return {root:section,sync(){},destroy(){section.remove();}};
    }
  });
})();
