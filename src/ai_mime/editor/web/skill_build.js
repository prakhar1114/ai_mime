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
    const runCmd = event.skill_dir
      ? `cd ${event.skill_dir} && ./run.sh`
      : "";
    banner.innerHTML = `
      <h2>Skill ready ✓</h2>
      <div>${escapeHtml(event.summary || "Skill package built and verified.")}</div>
      ${event.skill_dir ? `<div style="margin-top:6px;font-family:monospace">${escapeHtml(event.skill_dir)}</div>` : ""}
      ${runCmd ? `<div style="margin-top:10px">Run it:</div><pre><code>${escapeHtml(runCmd)}</code></pre>` : ""}
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

  // Handover pickup: when /replay/<id> hands off a failed script run, the
  // payload is left in sessionStorage. We start a fresh session and
  // auto-submit a healing prompt so the agent has the right context.
  (function handleReplayHandover() {
    const key = `replay:handover:${taskId}`;
    let payload = null;
    try {
      const raw = sessionStorage.getItem(key);
      if (!raw) return;
      payload = JSON.parse(raw);
    } catch { return; }
    if (!payload) return;
    try { sessionStorage.removeItem(key); } catch { /* ignore */ }

    function buildPrompt() {
      const paramLines = Object.entries(payload.params || {})
        .map(([k, v]) => `  ${k} = ${typeof v === "string" ? v : JSON.stringify(v)}`)
        .join("\n") || "  (no params)";
      const stdout = payload.stdoutTail || "(no stdout captured)";
      const stderr = payload.stderrTail || "(no stderr captured)";
      return [
        `The existing skill failed during replay and needs build_skill_chat healing.`,
        ``,
        `Skill directory: ${payload.skillDir || "(unknown skill dir)"}`,
        ``,
        `Params used:`,
        paramLines,
        ``,
        `Exit code: ${payload.exitCode == null ? "(unknown)" : payload.exitCode}`,
        `Error: ${payload.error || "(no error message)"}`,
        ``,
        `Recent stdout:`,
        "```",
        stdout,
        "```",
        ``,
        `Recent stderr:`,
        "```",
        stderr,
        "```",
        ``,
        `Combined log tail:`,
        "```",
        payload.logsTail || "(no logs captured)",
        "```",
        ``,
        `Please inspect and fix scripts/run.py / run.sh. After editing, run the end-to-end check to verify the skill.`,
      ].join("\n");
    }

    function trySubmit() {
      const newChatBtn = document.getElementById("newChatBtn");
      const messageInput = document.getElementById("messageInput");
      const chatForm = document.getElementById("chatForm");
      if (!messageInput || !chatForm) {
        // agent.js may not have mounted yet — retry shortly.
        setTimeout(trySubmit, 120);
        return;
      }
      // Start from a fresh session.
      try { newChatBtn && newChatBtn.click(); } catch { /* ignore */ }
      // Render a small handover banner so the user sees what's happening.
      if (banner) {
        banner.classList.remove("success");
        banner.classList.add("failure");
        banner.hidden = false;
        banner.innerHTML = `
          <h2>Healing the skill from a failed replay run</h2>
          <div>${escapeHtml(payload.error || "Script failed during replay.")}</div>
          <div style="margin-top:6px;font-family:monospace">${escapeHtml(payload.skillDir || "")}</div>
        `;
      }
      messageInput.value = buildPrompt();
      messageInput.dispatchEvent(new Event("input", { bubbles: true }));
      // Submit the form via requestSubmit so agent.js's submit handler runs.
      setTimeout(() => {
        if (typeof chatForm.requestSubmit === "function") {
          chatForm.requestSubmit();
        } else {
          chatForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
        }
      }, 80);
    }

    // Defer to next tick so agent.js (loaded later in skill_build.html order)
    // has a chance to mount its handlers.
    setTimeout(trySubmit, 0);
  })();
})();
