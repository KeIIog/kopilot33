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
      '10Hz 응답 지연을 식별해 작은 지연값 보정안을 만듭니다. 주행 중 값 변경 없음. P단에서만 1단계 실험 적용·원복 가능. 현대차 고정 Kp/Ki/Kf는 수정하지 않습니다.',
      `<div class="setting-profile-row">조향 자동분석 <label><input type="checkbox" data-role="steering"> ON</label></div>
       <div class="setting-profile-row">감속 자동분석 <label><input type="checkbox" data-role="deceleration"> ON</label></div>
       <div class="ui-action-grid"><button class="smallBtn" data-role="analyze">로그 분석</button><button class="smallBtn" data-role="proposal">지연 보정안 계산</button><button class="smallBtn" data-role="report">결과 다운로드</button></div>
       <div class="ui-action-grid"><button class="smallBtn" data-role="apply-steer">조향 1단계 실험 적용 (P단)</button><button class="smallBtn" data-role="apply-decel">감속 1단계 실험 적용 (P단)</button><button class="smallBtn" data-role="rollback">실험 원복 (P단)</button></div>
       <p class="muted mt-sm" style="white-space:pre-wrap" data-role="out">API 확인 중…</p>`);
    const out=v.c.querySelector('[data-role=out]');let busy=false;
    const st=v.c.querySelector('[data-role=steering]'), de=v.c.querySelector('[data-role=deceleration]');
    async function run(f){if(busy)return;busy=true;try{await f();}catch(e){out.textContent='실패: '+errorText(e);}finally{busy=false;}}
    async function load(){await run(async()=>{let s=await api('/api/ko/auto_tune/status');st.checked=!!s.config?.steering;de.checked=!!s.config?.deceleration;out.textContent=`버전 ${s.version} · 실측 기록 ${s.log?.enabled?'ON':'OFF'} (${s.log?.samples||0}개)\n적용 중인 실험: ${s.active_trial?`${s.active_trial.param}: ${s.active_trial.before} → ${s.active_trial.after}`:'없음'}`;});}
    function on(k,el){return ()=>run(async()=>{let d=await api('/api/ko/auto_tune/config',{[k]:el.checked});out.textContent=`${k}=${el.checked?'ON':'OFF'} · 로그 ${d.log?.enabled?'ON':'OFF'}`;});}
    async function analyze(){await run(async()=>{let d=await api('/api/ko/auto_tune/analyze',{});out.textContent=`조향: ${JSON.stringify(d.steering)}\n감속: ${JSON.stringify(d.deceleration)}\n보고서: ${d.report_file}`;});}
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
  const css=document.createElement('style');css.textContent='[data-settings-extension-panel="ko-door-control"]{display:none!important}';document.head.appendChild(css);
  ext.register({id:'ko-auto-tuning-v24',matches:c=>c.group==='VEH_AUX'&&!c.detailMode&&!!c.root,
    mount(c){const el=mountTune(c.root,c.lifecycle);return{root:el,sync(){},destroy(){el.remove();}};}});
  ext.register({id:'ko-door-test-v24',matches:c=>c.group==='VEH_AUX'&&!c.detailMode&&!!c.root,
    mount(c){const v=card(c.root,'ko-door-test-v24','도어락 송신 실험 + 실제 상태 검증',
      'P단·정지·시동 ON·보조제어 OFF 조건에서만 미확정 0x3FF 명령을 1회 실험합니다. 충전 케이블 분리, 차량 주변 안전 확인. Panda Safety 우회 없음. 예상과 다른 차량 반응 가능.',
      `<div class="ui-action-grid"><button type="button" class="smallBtn" data-role="state">현재 잠금 상태</button><button type="button" class="smallBtn" data-role="lock">실험 잠금 송신</button><button type="button" class="smallBtn" data-role="unlock">실험 잠금 해제 송신</button><button type="button" class="smallBtn" data-role="logs">실험 로그 다운로드</button></div><p class="muted mt-sm" style="white-space:pre-wrap" data-role="out">조회 대기</p>`);
      const out=v.c.querySelector('[data-role=out]');let busy=false;
      async function wrap(cb){if(busy)return;busy=true;try{await cb();}catch(e){out.textContent='실험 실패: '+errorText(e);}finally{busy=false;}}
      async function state(){await wrap(async()=>{const d=await api('/api/ko/door/state');out.textContent='잠금 상태: '+JSON.stringify(d.door_state,null,2);});}
      async function trial(action){if(!(await ask(`⚠️ 미확인 0x3FF CAN 명령 실험입니다. 충전선 분리, P단, 정지, 주변 안전 확인 후 ${action==='lock'?'잠금':'해제'}를 1회 시도하시겠습니까? 차량 잠금이 아닌 다른 기능이 반응할 수 있습니다.`)))return;
        await wrap(async()=>{const res=await fetch('/api/ko/door/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({experimental:true,confirmation:'ONE_SHOT_PARKED_3FF'})});let d=await res.json();out.textContent=`${res.status} ${d.ok?'차량 상태 전환 확인':'잠금 구동 미확인'}\nCAN TX: ${JSON.stringify(d.panda_tx||{})}\n이전: ${JSON.stringify(d.door_state_before||{})}\n이후: ${JSON.stringify(d.door_state_after||{})}\n오류: ${d.error||''}\n실험 로그 저장됨`;});}
      const cb={state:()=>void state(),lock:()=>void trial('lock'),unlock:()=>void trial('unlock'),logs:()=>{window.location.href='/api/ko/door/experiment_log';}};
      for(const [k,fn] of Object.entries(cb)){const el=v.c.querySelector(`[data-role=${k}]`);el.addEventListener('click',fn);c.lifecycle.addCleanup(()=>el.removeEventListener('click',fn));}
      return{root:v.sec,sync(){},destroy(){v.sec.remove();}};
    }});
})();
