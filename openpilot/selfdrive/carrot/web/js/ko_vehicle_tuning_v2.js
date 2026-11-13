/* KOPilot v2.4 experimental parked tuning and door 0x3FF test panel. */
(function () {
  'use strict';
  const ext = window.CarrotSettingsExtensions;
  if (!ext || typeof ext.register !== 'function') return;
  async function api(url, obj) {
    const r = await fetch(url,{cache:'no-store',method:obj===undefined?'GET':'POST',
      headers:{'Content-Type':'application/json'},body:obj===undefined?undefined:JSON.stringify(obj)});
    const raw=await r.text(); let json;
    try{json=JSON.parse(raw);}catch{throw Error(`HTTP ${r.status}: ${raw.slice(0,100)||'JSON 아닌 서버 응답'}. comma 4 웹서버 재시작 확인`);}
    if(!r.ok || json.ok===false) throw Error(json.error||`HTTP ${r.status}`);
    return json;
  }
  function card(root,id,title,desc,body){
    const sec=document.createElement('section');sec.className='setting-section-block ui-stagger-item';
    sec.dataset.settingsExtensionPanel=id;
    const c=document.createElement('div');c.className='setting-group-card';
    c.innerHTML=`<div class="setting-profile-row"><div><strong>${title}</strong><p class="muted mt-sm">${desc}</p></div></div>${body}`;
    sec.appendChild(c);root.appendChild(sec);return {sec,c};
  }
  function ask(message){return typeof window.appConfirm==='function'?window.appConfirm(message):Promise.resolve(window.confirm(message));}
  function errorText(e){return e?.message||String(e);}
  function mountTune(root,lifecycle){
    const v=card(root,'ko-autotune-v24','조향 · 감속 자동튜닝 — 1단계 실측 보정',
      '조향/감속은 지연 보정 후보만 만들며 P단에서 수동 적용합니다. 차간거리는 분석만 지원하며 자동 변경하지 않습니다. Kp/Ki/Kf는 자동 변경하지 않습니다.',
      `<div class="setting-profile-row">조향 자동분석 <label><input type="checkbox" data-role="steering"> ON</label></div>
       <div class="setting-profile-row">감속 자동분석 <label><input type="checkbox" data-role="deceleration"> ON</label></div>
       <div class="ui-action-grid"><button class="smallBtn" data-role="analyze">로그 분석</button><button class="smallBtn" data-role="proposal">지연 보정안 계산</button><button class="smallBtn" data-role="report">결과 다운로드</button></div>
       <div class="ui-action-grid"><button class="smallBtn" data-role="apply-steer">조향 1단계 실험 적용 (P단)</button><button class="smallBtn" data-role="apply-decel">감속 1단계 실험 적용 (P단)</button><button class="smallBtn" data-role="rollback">실험 원복 (P단)</button></div>
       <p class="muted mt-sm" style="white-space:pre-wrap" data-role="out">API 확인 중…</p>`);
    const out=v.c.querySelector('[data-role=out]');let busy=false;
    const st=v.c.querySelector('[data-role=steering]'), de=v.c.querySelector('[data-role=deceleration]');
    async function run(f){if(busy)return;busy=true;try{await f();}catch(e){const m=errorText(e);out.textContent=m.startsWith('no qualified proposal:')?'보정안 없음 (오류 아님): '+m.slice('no qualified proposal:'.length)+'\n10Hz 지연 식별 불가 또는 보정 불필요. 값 변경 없음.':'실패: '+m;}finally{busy=false;}}
    async function load(){await run(async()=>{let s=await api('/api/ko/auto_tune/status');st.checked=!!s.config?.steering;de.checked=!!s.config?.deceleration;out.textContent=`버전 ${s.version} · 실측 기록 ${s.log?.enabled?'ON':'OFF'} (${s.log?.samples||0}개)\n자동 튜닝값 적용: OFF (설계상 수동 1단계)\n현재 실제 Params: ${JSON.stringify(s.current_params||{})}\n적용 중인 실험: ${s.active_trial?`${s.active_trial.param}: ${s.active_trial.before} → ${s.active_trial.after}`:'없음'}`;});}
    function on(k,el){return ()=>run(async()=>{let d=await api('/api/ko/auto_tune/config',{[k]:el.checked});out.textContent=`${k}=${el.checked?'ON':'OFF'} · 로그 ${d.log?.enabled?'ON':'OFF'}`;});}
    async function analyze(){await run(async()=>{let d=await api('/api/ko/auto_tune/analyze',{});out.textContent=`조향: ${JSON.stringify(d.steering)}\n감속: ${JSON.stringify(d.deceleration)}\n차간거리: ${JSON.stringify(d.following)}\n현재 Params: ${JSON.stringify(d.current_params)}\n자동 적용: 없음\n보고서: ${d.report_file}`;});}
    async function propose(){await run(async()=>{const d=await api('/api/ko/auto_tune/proposal');out.textContent=`실험 가능: ${d.can_apply?'YES':'NO'}\n보정안: ${JSON.stringify(d.proposals||{},null,2)}\n보류: ${JSON.stringify(d.skipped||d.reason||{},null,2)}\n${d.note||''}`;});}
    async function apply(axis){if(!(await ask(`${axis==='steering'?'조향':'감속'} 지연값을 P단에서 1단계 변경합니다. 실험주행 후 문제가 있으면 즉시 기존 설정으로 원복하세요. 계속할까요?`)))return;
      await run(async()=>{let d=await api('/api/ko/auto_tune/apply_trial',{axis,confirmation:'APPLY_ONE_STEP_WHILE_PARKED'});out.textContent=`실험 적용: ${d.trial.param} ${d.trial.before} → ${d.trial.after}\n다음 주행 중 조작하지 말고, 실험 후 P단에서 원복할 수 있습니다.`;});}
    async function rollback(){if(!(await ask('P단에서 직전 실험 설정을 이전 값으로 원복할까요?')))return;
      await run(async()=>{let d=await api('/api/ko/auto_tune/rollback_trial',{confirmation:'ROLLBACK_WHILE_PARKED'});out.textContent='원복 완료: '+JSON.stringify(d.restored);});}
    const bindings=[
      [st,'change',on('steering',st)],[de,'change',on('deceleration',de)],
      [v.c.querySelector('[data-role=analyze]'),'click',()=>void analyze()],
      [v.c.querySelector('[data-role=proposal]'),'click',()=>void propose()],
      [v.c.querySelector('[data-role=report]'),'click',()=>{window.location.href='/api/ko/auto_tune/report';}],
      [v.c.querySelector('[data-role=apply-steer]'),'click',()=>void apply('steering')],
      [v.c.querySelector('[data-role=apply-decel]'),'click',()=>void apply('deceleration')],
      [v.c.querySelector('[data-role=rollback]'),'click',()=>void rollback()],
    ];
    for(const [el,evt,fn] of bindings){el.addEventListener(evt,fn);lifecycle.addCleanup(()=>el.removeEventListener(evt,fn));}
    void load();return v.sec;
  }
  // Hide legacy "Ready" (it only meant Panda allowed TX, not actual door actuation).
  /* KO_DOOR_BUTTONS_VISIBLE_20261031 */
  ext.register({id:'ko-auto-tuning-v24',matches:c=>c.group==='VEH_AUX'&&!c.detailMode&&!!c.root,
    mount(c){const el=mountTune(c.root,c.lifecycle);return{root:el,sync(){},destroy(){el.remove();}};}});
  ext.register({id:'ko-door-physical-v252',matches:c=>c.group==='VEH_AUX'&&!c.detailMode&&!!c.root,
    mount(c){const v=card(c.root,'ko-door-physical-v252','도어 CAN 물리버튼 비교 녹화 (송신 없음)',
      '3FF 제어 실험은 중단했습니다. 시작 버튼을 누른 뒤 12초 동안 실물 스마트키로 잠금 또는 잠금해제를 한 번 누르세요. 4A2/587과 진단 후보는 수신만 기록합니다.',
      `<div class="ui-action-grid"><button type="button" class="smallBtn" data-role="state">현재 잠금 상태</button><button type="button" class="smallBtn" data-role="lock">물리 잠금 기록 시작 (12초)</button><button type="button" class="smallBtn" data-role="unlock">물리 잠금해제 기록 시작 (12초)</button><button type="button" class="smallBtn" data-role="logs">전체 CAN 압축 로그</button><button type="button" class="smallBtn" data-role="status">후보별 수신 결과</button></div><p class="muted mt-sm" style="white-space:pre-wrap" data-role="out">실차 연결이 필요합니다. 이 페이지에서 차량으로 명령을 전송하지 않습니다.</p>`);
      const out=v.c.querySelector('[data-role=out]');let busy=false;
      async function wrap(fn){if(busy)return;busy=true;try{await fn();}catch(e){out.textContent='실패: '+errorText(e);}finally{busy=false;}}
      async function state(){await wrap(async()=>{const d=await api('/api/ko/door/state');out.textContent='잠금 상태: '+JSON.stringify(d.door_state,null,2);});}
      async function capture(action){if(!(await ask(`이 기능은 차량 명령을 전송하지 않습니다. 실차에 comma 4를 연결하고, 기록을 시작한 후 12초 안에 스마트키에서 ${action==='lock'?'잠금':'잠금 해제'}를 직접 누르겠습니까?`)))return;
        await wrap(async()=>{out.textContent='12초 동안 수신 기록 중... 지금 물리 스마트키 버튼을 한 번 누르세요.';
          const res=await fetch('/api/ko/door/probe/capture',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,confirmation:'PHYSICAL_BUTTON_CAPTURE_NO_TX'})});
          const raw=await res.text();let d;try{d=JSON.parse(raw);}catch{throw Error('HTTP '+res.status+': '+raw.slice(0,120));}
          out.textContent=(d.ok?'수신 완료':'실차 CAN 수신 실패')+' · 총 '+(d.frames_written||0)+'프레임 · 잘림 '+(d.truncated?'YES':'NO')+' · 차량송신 '+d.can_transmitted+'\n잠금 지표: '+JSON.stringify(d.inferred_door_state||{})+'\n후보 CAN 수신: '+JSON.stringify(d.candidate_counts||{},null,2)+'\n후보 페이로드 변화: '+JSON.stringify(d.candidate_payload_changes||{},null,2)+'\n오류: '+(d.capture_error||'없음')+'\n저장 파일: '+(d.file||'없음');
        });}
      async function status(){await wrap(async()=>{let d=await api('/api/ko/door/probe/status');out.textContent=JSON.stringify(d.latest,null,2);});}
      async function logs(){await wrap(async()=>{const r=await fetch('/api/ko/door/probe/download',{cache:'no-store'});
        if(!r.ok){throw Error('로그 다운로드 HTTP '+r.status+': '+(await r.text()).slice(0,200));}
        const disposition=r.headers.get('Content-Disposition')||'';
        const file=(disposition.match(/filename=\"?([^\";]+)\"?/i)||[])[1]||'door_probe_latest.jsonl.gz';
        const blob=await r.blob();const href=URL.createObjectURL(blob);
        const a=document.createElement('a');a.href=href;a.download=file;document.body.appendChild(a);a.click();a.remove();
        setTimeout(()=>URL.revokeObjectURL(href),2500);
        out.textContent='압축 로그 다운로드 요청: '+file+' ('+blob.size+' bytes)';});}
      const cb={state:()=>void state(),lock:()=>void capture('lock'),unlock:()=>void capture('unlock'),logs:()=>void logs(),status:()=>void status()};
      for(const [k,fn] of Object.entries(cb)){const el=v.c.querySelector(`[data-role=${k}]`);el.addEventListener('click',fn);c.lifecycle.addCleanup(()=>el.removeEventListener('click',fn));}
      return{root:v.sec,sync(){},destroy(){v.sec.remove();}};
    }});
})();

/* KO_GAP_TUNE_STATE_REPORT_20261031 */

/* KO_DOOR_PREVIEW_PANEL_V254 */
(() => {
  'use strict';

  const ext = window.CarrotSettingsExtensions;
  if (!ext || typeof ext.register !== 'function') return;

  const style = document.createElement('style');
  style.textContent =
    '[data-settings-extension-panel="ko-door-physical-v252"]{display:none!important}';
  document.head.appendChild(style);

  async function read(url) {
    const r = await fetch(url, {
      method: 'GET',
      cache: 'no-store'
    });
    const d = await r.json();
    if (!r.ok || d?.ok === false)
      throw Error(d?.error || `HTTP ${r.status}`);
    return d;
  }

  ext.register({
    id: 'ko-door-preview-v254',
    matches: c =>
      c.group === 'VEH_AUX' &&
      !c.detailMode &&
      !!c.root,

    mount(c) {
      const section = document.createElement('section');
      section.className =
        'setting-section-block ui-stagger-item';

      section.dataset.settingsExtensionPanel =
        'ko-door-preview-v254';

      const card = document.createElement('div');
      card.className = 'setting-group-card';

      card.innerHTML = `
        <div class="setting-profile-row">
          <div>
            <strong>KO 도어 CAN 후보 확인</strong>
            <p class="muted mt-sm">
              읽기 전용 미리보기입니다.
              실제 도어 잠금·해제 명령을 송신하지 않습니다.
            </p>
          </div>
        </div>
        <div class="ui-action-grid">
          <button class="smallBtn" data-role="lock">
            잠금 후보 확인
          </button>
          <button class="smallBtn" data-role="unlock">
            잠금해제 후보 확인
          </button>
          <button class="smallBtn" data-role="state">
            현재 잠금 상태
          </button>
        </div>
        <p class="muted mt-sm"
           style="white-space:pre-wrap"
           data-role="out">
          대기 중 · CAN 송신 없음
        </p>
      `;

      section.appendChild(card);
      c.root.appendChild(section);

      const out = card.querySelector('[data-role=out]');
      const buttons = Array.from(
        card.querySelectorAll('button')
      );

      let busy = false;

      async function run(fn) {
        if (busy) return;
        busy = true;
        buttons.forEach(b => b.disabled = true);

        try {
          await fn();
        } catch (e) {
          out.textContent =
            '조회 실패: ' + e.message +
            '\nCAN 송신 없음';
        } finally {
          busy = false;
          buttons.forEach(b => b.disabled = false);
        }
      }

      function candidate(action) {
        return run(async () => {
          const d = await read(
            '/api/ko/door/candidate/' + action
          );
          out.textContent =
            (action === 'lock' ? '잠금' : '잠금해제') +
            ' 후보 조회 완료\n' +
            '미리보기 전용 · 차량 CAN 송신 없음\n\n' +
            JSON.stringify(d, null, 2).slice(0, 5000);
        });
      }

      function state() {
        return run(async () => {
          const d = await read('/api/ko/door/state');
          const st = d.door_state || {};
          out.textContent =
            '잠금 상태 추정: ' +
            (st.state || 'unknown') +
            '\n교차 확인: ' +
            (st.corroborated === true ? '일치' : '불충분') +
            '\n차량 CAN 송신 없음';
        });
      }

      const actions = [
        () => candidate('lock'),
        () => candidate('unlock'),
        () => state()
      ];

      buttons.forEach((b, i) => {
        b.addEventListener('click', actions[i]);
        c.lifecycle.addCleanup(() =>
          b.removeEventListener('click', actions[i])
        );
      });

      return {
        root: section,
        sync() {},
        destroy() { section.remove(); }
      };
    }
  });
})();
