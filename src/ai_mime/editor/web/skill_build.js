(() => {
  const shell = document.querySelector(".agent-shell");
  const taskId = shell && shell.dataset ? shell.dataset.taskId : "";
  const banner = document.getElementById("terminalBanner");
  if (!banner) return;

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function renderSuccess(event) {
    banner.classList.remove("failure");
    banner.classList.add("success");
    banner.hidden = false;
    const logs = event.e2e_logs ? `<pre>${escapeHtml(event.e2e_logs)}</pre>` : "";
    banner.innerHTML = `
      <h2>Skill ready ✓</h2>
      <div>${escapeHtml(event.summary || "Skill package built and verified.")}</div>
      ${event.skill_dir ? `<div style="margin-top:6px;font-family:monospace">${escapeHtml(event.skill_dir)}</div>` : ""}
      ${logs}
    `;
  }

  function renderUnbuildable(event) {
    banner.classList.remove("success");
    banner.classList.add("failure");
    banner.hidden = false;
    const bullets = Array.isArray(event.suggested_changes) && event.suggested_changes.length
      ? `<ul>${event.suggested_changes.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ul>`
      : "";
    banner.innerHTML = `
      <h2>Skill unbuildable</h2>
      <div>${escapeHtml(event.reason || event.summary || "Workflow cannot be made deterministic.")}</div>
      ${bullets}
      <div class="actions">
        <button id="retryBuildBtn">Retry with a new session</button>
      </div>
    `;
    const retry = document.getElementById("retryBuildBtn");
    if (retry) {
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        try {
          await fetch(`/api/tasks/${encodeURIComponent(taskId)}/skill-build/reset`, { method: "POST" });
          banner.hidden = true;
          banner.innerHTML = "";
          document.getElementById("newChatBtn")?.click();
        } catch {
          retry.disabled = false;
        }
      });
    }
  }

  function renderCheckFailed(event) {
    banner.classList.remove("success");
    banner.classList.add("failure");
    banner.hidden = false;
    const logs = event.logs ? `<pre>${escapeHtml(event.logs)}</pre>` : "";
    banner.innerHTML = `
      <h2>Skill check failed — keep iterating</h2>
      <div>${escapeHtml(event.error || "Validation or e2e test failed.")}</div>
      ${event.skill_dir ? `<div style="margin-top:6px;font-family:monospace">${escapeHtml(event.skill_dir)}</div>` : ""}
      ${logs}
    `;
  }

  window.addEventListener("agent-stream-event", (e) => {
    const event = e && e.detail ? e.detail : null;
    if (!event) return;
    if (event.event === "skill_build_done") {
      if (event.status === "skill_ready") renderSuccess(event);
      else renderUnbuildable(event);
    } else if (event.event === "skill_check_failed") {
      renderCheckFailed(event);
    }
  });

  // On load: surface an existing terminal status, if any.
  (async () => {
    try {
      const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/skill-build/sessions`);
      if (!res.ok) return;
      const data = await res.json();
      if (data && data.terminal_status === "skill_ready") {
        renderSuccess({ summary: "Previously completed.", skill_dir: data.skill_dir });
      } else if (data && data.terminal_status === "skill_unbuildable") {
        renderUnbuildable({ reason: "Previously declared unbuildable.", suggested_changes: [] });
      }
    } catch {
      // ignore
    }
  })();
})();
