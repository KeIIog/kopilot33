/* KOPilot Tune/door diagnostic v2.3: web UI only; does not command steering, brakes or CAN. */
(function () {
'use strict';
const EXT = window.CarrotSettingsExtensions;
if (!EXT || typeof EXT.register !== 'function') return;
const escapeHtml = s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function req(url, data) {
  const response = await fetch(url, {cache:'no-store',method:data===undefined?'GET':'POST',
    headers:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});
  const raw=await response.text();
  let result;
  try {result=JSON.parse(raw);} catch {
    const hint=response.status===404?' 서버 모듈이 아직 로드되지 않았습니다. 안전 주차 후 comma 4를 재부팅하세요.':'';
    throw Error(`API 응답이 JSON이 아님 (HTTP ${response.status}).${hint}`);
  }
  if (!response.ok || result.ok===false) throw Error(result.error||`HTTP ${response.status}`);
  return result;
}
function mountTune(root, lifecycle) {
  const doc=root.ownerDocument, section=doc.createElement('section');
  section.className='setting-section-block ui-stagger-item';
  section.dataset.settingsExtensionPanel='ko-auto-tuning-v23';
  const card=doc.createElement('div'); card.className='setting-group-card';
  card.innerHTML=`<div class="setting-profile-card__rows">
    <div class="setting-profile-row"><div><strong>조향·감속 자동튜닝 (실측/검증 모드)</strong><p class="muted mt-sm">ON → 10Hz 주행 기록 자동 시작 → 오차·응답지연 자동 분석. 실제 조향/제동 게인은 변경하지 않습니다.</p></div></div>
    <div class="setting-profile-row"><div>조향 응답 식별</div><label><input type="checkbox" data-role="st"> ON</label></div>
    <div class="setting-profile-row"><div>감속 응답 식별</div><label><input type="checkbox" data-role="de"> ON</label></div>
    <div class="ui-action-grid"><button class="smallBtn" type="button" data-role="run">누적 로그 분석</button>
    <button class="smallBtn" type="button" data-role="download">분석 결과 다운로드</button>
    <button class="smallBtn" type="button" data-role="raw">주행 원본 로그 다운로드</button></div>
    <p class="muted mt-sm" data-role="status" style="white-space:pre-wrap">서버 연결 확인 중…</p></div>`;
  section.appendChild(card);root.appendChild(section);
  const st=card.querySelector('[data-role=st]'),de=card.querySelector('[data-role=de]');
  const status=card.querySelector('[data-role=status]'),run=card.querySelector('[data-role=run]');
  let busy=false,active=false;
  const fmt=(name,s)=>s?.state==='analyzed'?`${name}: ${s.samples}점, RMS ${s.rms_error} ${s.unit}, 95%오차 ${s.p95_absolute_error}, 지연후보 ${s.estimated_lag_ms??'?'}ms`:
                  `${name}: ${s?.state||'대기'} · ${s?.samples||0}점`;
  async function load() {
    try {
      const data=await req('/api/ko/auto_tune/status');
      if (lifecycle.destroyed) return;
      st.checked=!!data.config?.steering;de.checked=!!data.config?.deceleration;
      active=!!data.log?.enabled;
      status.textContent=`백엔드 ${data.version||'?'} · 10Hz 기록 ${active?'ON':'OFF'} · ${data.log?.samples||0}점\n계수 자동적용 OFF · 제어기 안전 설정 유지`;
    } catch(e){status.textContent=`자동튜닝 연결 오류: ${e.message}`;}
  }
  async function change(key,value) {
    if(busy)return;busy=true;st.disabled=de.disabled=true;
    try {const d=await req('/api/ko/auto_tune/config',{[key]:value});
      status.textContent=`설정 저장 완료 · 로그 자동 기록 ${d.log?.enabled?'ON':'OFF'}\n정차한 상태에서 업데이트가 필요하면 재부팅 후 확인.`;
    } catch(e) {status.textContent=`설정 실패: ${e.message}`;if(key==='steering')st.checked=!value;else de.checked=!value;}
    finally{busy=false;st.disabled=de.disabled=false;}
  }
  async function analyze() {
    if(busy)return;busy=true;run.disabled=true;status.textContent='10Hz 로그에서 조향·감속 응답 분석 중…';
    try{const d=await req('/api/ko/auto_tune/analyze',{});
      status.textContent=`${fmt('조향',d.steering)}\n${fmt('감속',d.deceleration)}\n보고서: ${d.report_file||'미저장'}\n실제 제어 계수 변경 없음`;
    }catch(e){status.textContent='분석 실패: '+e.message;}
    finally{busy=false;run.disabled=false;}
  }
  function download(which){window.location.href=which==='report'?'/api/ko/auto_tune/report':'/api/ko/drive_log/download';}
  const onS=()=>void change('steering',st.checked),onD=()=>void change('deceleration',de.checked),
        onR=()=>void analyze(),onRep=()=>download('report'),onRaw=()=>download('raw');
  const rep=card.querySelector('[data-role=download]'),raw=card.querySelector('[data-role=raw]');
  st.addEventListener('change',onS);de.addEventListener('change',onD);run.addEventListener('click',onR);
  rep.addEventListener('click',onRep);raw.addEventListener('click',onRaw);
  lifecycle.addCleanup(()=>{st.removeEventListener('change',onS);de.removeEventListener('change',onD);
    run.removeEventListener('click',onR);rep.removeEventListener('click',onRep);raw.removeEventListener('click',onRaw);});
  void load(); return section;
}
EXT.register({id:'ko-auto-tuning-v23',matches:c=>c.group==='VEH_AUX'&&!c.detailMode&&!!c.root,
  mount(c){const section=mountTune(c.root,c.lifecycle);return {root:section,sync(){},destroy(){section.remove();}};}});
// Old UI wrongly indicates "Ready" for the unverified 0x3FF actuation. Hide it.
const css=document.createElement('style');css.textContent='[data-settings-extension-panel="ko-door-control"]{display:none!important}';document.head.appendChild(css);
EXT.register({id:'ko-door-diagnostic-v23',matches:c=>c.group==='VEH_AUX'&&!c.detailMode&&!!c.root,
  mount(c){const doc=c.root.ownerDocument,section=doc.createElement('section');
    section.className='setting-section-block ui-stagger-item';section.dataset.settingsExtensionPanel='ko-door-diagnostic-v23';
    const card=doc.createElement('div');card.className='setting-group-card';
    card.innerHTML='<div class="setting-profile-row"><div><strong>도어락 CAN 진단</strong><p class="muted mt-sm">0x411·0x414 잠금 상태 교차 확인. 0x3FF는 구동 명령인지 확인되지 않아 실제 전송을 중지한 상태입니다.</p></div></div><p class="muted mt-sm" data-role="state">차량 상태 확인 중…</p><div class="ui-action-grid"><button type="button" class="smallBtn" data-role="refresh">실시간 상태 측정</button><button type="button" class="smallBtn" disabled>문 잠금: 미검증</button><button type="button" class="smallBtn" disabled>잠금 해제: 미검증</button></div>';
    section.appendChild(card);c.root.appendChild(section);
    const output=card.querySelector('[data-role=state]'),btn=card.querySelector('[data-role=refresh]');
    async function refresh(){btn.disabled=true;try{const d=await req('/api/ko/door/state');
      const s=d.door_state||{};output.textContent=`잠금 상태(교차 추정): ${s.state||'unknown'} · 수신 0x411=${s.frames_411||0} / 0x414=${s.frames_414||0}\n실제 도어 구동 명령 미확인. CAN 임의 전송 없음.`;
    }catch(e){output.textContent='도어 상태 조회 오류: '+e.message;}finally{btn.disabled=false;}}
    const onClick=()=>void refresh();btn.addEventListener('click',onClick);
    c.lifecycle.addCleanup(()=>btn.removeEventListener('click',onClick));void refresh();
    return {root:section,sync(){},destroy(){section.remove();}};
  }});
})();
