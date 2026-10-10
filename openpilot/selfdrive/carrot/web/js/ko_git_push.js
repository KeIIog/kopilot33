/* KO Web Git Commands extension - independent of bundled tools.js (no npm needed).
   Only explicit, confirmed clicks initiate a remote git operation. */
(() => {
  "use strict";
  const actions = [
    {id: "btnGitPush", name: "git_push", title: "Git Push",
     question: "현재 test 브랜치의 소스 수정사항을 커밋하고 GitHub에 Push할까요? 로그와 미추적 파일은 제외됩니다."},
    {id: "btnGitPushUndo", name: "git_push_undo", title: "Undo Push",
     question: "마지막 KO Web Push를 새로운 되돌리기 커밋으로 취소할까요? GitHub 기록을 강제로 삭제하지 않습니다."},
  ];

  async function getJson(url, options) {
    const response = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
    });
    let body;
    try { body = await response.json(); }
    catch { throw new Error(`Server returned HTTP ${response.status}`); }
    if (!response.ok || body.ok === false) {
      throw new Error(body.error || body.message || `HTTP ${response.status}`);
    }
    return body;
  }

  async function startAction(button, action) {
    if (button.dataset.gitBusy === "1") return;
    if (!window.confirm(action.question)) return;
    button.dataset.gitBusy = "1";
    button.disabled = true;
    const label = button.textContent;
    button.textContent = "진행 중…";
    try {
      const job = await getJson("/api/tools/start", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action: action.name}),
      });
      if (!job.job_id) throw new Error("Server did not start a job");
      let finished = false;
      for (let step = 0; step < 300; step++) {
        await new Promise(resolve => setTimeout(resolve, 800));
        const data = await getJson(`/api/tools/job?id=${encodeURIComponent(job.job_id)}`);
        if (!data.done) continue;
        finished = true;
        const result = data.result || {};
        if (data.status !== "done" || result.ok === false) {
          throw new Error(result.error || data.error || data.log || "Command failed");
        }
        const summary = result.out || data.log || "작업 완료";
        window.alert(`${action.title} 완료\n\n${summary}`);
        break;
      }
      if (!finished) throw new Error("아직 실행 중입니다. 도구 작업 기록에서 결과를 확인하세요.");
    } catch (error) {
      window.alert(`${action.title} 실패\n\n${error.message || error}`);
      console.error("[KO git extension]", error);
    } finally {
      button.textContent = label;
      button.disabled = false;
      delete button.dataset.gitBusy;
    }
  }

  function bind() {
    for (const action of actions) {
      const button = document.getElementById(action.id);
      if (!button || button.dataset.gitBound === "1") continue;
      button.dataset.gitBound = "1";
      button.addEventListener("click", () => startAction(button, action));
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
