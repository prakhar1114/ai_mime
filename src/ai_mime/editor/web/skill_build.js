(() => {
  const shell = document.querySelector(".agent-shell");
  const taskId = shell && shell.dataset ? shell.dataset.taskId : "";
  const banner = document.getElementById("terminalBanner");
  if (!banner) return;
  const startPanel = document.getElementById("skillBuildStart");
  const startTitle = document.getElementById("skillBuildStartTitle");
  const startCopy = document.getElementById("skillBuildStartCopy");
  const improveSkillBtn = document.getElementById("improveSkillBtn");
  const continueBtn = document.getElementById("continueBtn");
  const newSessionBtn = document.getElementById("newSkillBuildSessionBtn");
  let agentSessionsLoaded = false;

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

  function setStartButtonsDisabled(disabled) {
    if (improveSkillBtn) improveSkillBtn.disabled = !!disabled;
    if (continueBtn) continueBtn.disabled = !!disabled;
    if (newSessionBtn) newSessionBtn.disabled = !!disabled;
  }

  function hideStartPanel() {
    if (startPanel) startPanel.hidden = true;
  }

  function submitAgentPrompt(prompt) {
    const messageInput = document.getElementById("messageInput");
    const chatForm = document.getElementById("chatForm");
    if (!messageInput || !chatForm) return;
    messageInput.value = prompt;
    messageInput.dispatchEvent(new Event("input", { bubbles: true }));
    if (typeof chatForm.requestSubmit === "function") {
      chatForm.requestSubmit();
    } else {
      chatForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    }
  }

  function renderStartPanel(data) {
    if (!startPanel || !improveSkillBtn || !continueBtn || !newSessionBtn) return;
    const hasSkill = !!(data && data.has_skill);
    const hasPlan = data && data.has_optimized_plan !== false;
    if (!hasPlan) {
      startPanel.hidden = true;
      return;
    }
    startPanel.hidden = false;

    const hasActiveSession = !!(data && data.active_session_id);
    const isTerminated = !!(data && data.terminal_status);
    const isIncomplete = hasActiveSession && !isTerminated;

    improveSkillBtn.hidden = !hasSkill;
    continueBtn.hidden = !isIncomplete;

    if (startTitle) {
      if (hasSkill && isIncomplete) {
        startTitle.textContent = "Improve or continue building this skill";
      } else if (hasSkill) {
        startTitle.textContent = "Improve the existing skill";
      } else if (isIncomplete) {
        startTitle.textContent = "Continue building this skill";
      } else {
        startTitle.textContent = "Build this skill";
      }
    }

    if (startCopy) {
      if (hasSkill && isIncomplete) {
        startCopy.textContent = "Resume the active session to continue building, or start improving the existing skill package.";
      } else if (hasSkill) {
        startCopy.textContent = "The skill has been built. You can start a session to improve it or build from scratch.";
      } else if (isIncomplete) {
        startCopy.textContent = "Resume the active build session from the optimized workflow plan.";
      } else {
        startCopy.textContent = "Start a new skill-build session to iteratively turn this workflow into a reusable, deterministic skill.";
      }
    }

    setStartButtonsDisabled(!agentSessionsLoaded);
  }

  let actionProcessed = false;
  async function processUrlAction(activeSessionId) {
    if (actionProcessed) return;
    const urlParams = new URLSearchParams(window.location.search);
    const action = urlParams.get("action");
    if (!action) return;
    actionProcessed = true;

    // Strip action from URL
    try {
      const cleanUrl = window.location.protocol + "//" + window.location.host + window.location.pathname;
      window.history.replaceState({ path: cleanUrl }, "", cleanUrl);
    } catch (err) {
      // ignore
    }

    if (action === "new") {
      hideStartPanel();
      if (banner) {
        banner.hidden = true;
        banner.innerHTML = "";
      }
      try {
        await fetch(`/api/tasks/${encodeURIComponent(taskId)}/skill-build/reset`, { method: "POST" });
      } catch (err) {
        console.error("Failed to reset skill build:", err);
      }
      if (typeof window.AgentChat?.newChat === "function") {
        window.AgentChat.newChat();
        setTimeout(() => {
          const emptyState = document.querySelector("#messages .empty-state");
          if (emptyState) {
            emptyState.textContent = "Start by telling how you would like to edit the skill.";
          }
          const input = document.getElementById("messageInput");
          if (input) {
            input.placeholder = "Describe how you would like to edit the skill...";
            input.focus();
          }
        }, 150);
      }
    } else if (action === "continue") {
      hideStartPanel();
      if (activeSessionId) {
        if (typeof window.AgentChat?.loadMessages === "function") {
          window.AgentChat.loadMessages(activeSessionId);
          setTimeout(() => {
            submitAgentPrompt("continue");
          }, 150);
        }
      } else {
        if (typeof window.AgentChat?.newChat === "function") {
          window.AgentChat.newChat();
          setTimeout(() => {
            submitAgentPrompt("Start");
          }, 150);
        }
      }
    }
  }

  window.addEventListener("agent-sessions-loaded", (e) => {
    agentSessionsLoaded = true;
    setStartButtonsDisabled(false);
    const activeSessionId = e.detail && e.detail.active_session_id;
    processUrlAction(activeSessionId);
  });

  window.addEventListener("agent-session-loaded", (e) => {
    const sessionId = e.detail && e.detail.session_id;
    if (sessionId) {
      hideStartPanel();
    }
  });
  setTimeout(() => {
    agentSessionsLoaded = true;
    setStartButtonsDisabled(false);
  }, 1000);

  if (improveSkillBtn) {
    improveSkillBtn.addEventListener("click", async () => {
      if (!agentSessionsLoaded) return;

      improveSkillBtn.disabled = true;
      hideStartPanel();

      try {
        await fetch(`/api/tasks/${encodeURIComponent(taskId)}/skill-build/reset`, { method: "POST" });
        banner.hidden = true;
        banner.innerHTML = "";
      } catch (err) {
        console.error("Failed to reset skill build terminal status:", err);
      }

      const newChatBtn = document.getElementById("newChatBtn");
      try { newChatBtn && newChatBtn.click(); } catch { /* ignore */ }

      const emptyState = document.querySelector("#messages .empty-state");
      if (emptyState) {
        emptyState.textContent = "How would you like to improve this skill? Describe the changes or improvements you want to make, and the agent will update the existing skill package.";
      }

      const input = document.getElementById("messageInput");
      if (input) {
        input.placeholder = "Describe how to improve the skill...";
        input.focus();
      }
    });
  }

  const mainNewChatBtn = document.getElementById("newChatBtn");
  if (mainNewChatBtn) {
    mainNewChatBtn.addEventListener("click", () => {
      const input = document.getElementById("messageInput");
      if (input) input.placeholder = "Write a message...";
    });
  }

  if (continueBtn) {
    continueBtn.addEventListener("click", () => {
      if (!agentSessionsLoaded) return;
      continueBtn.disabled = true;
      hideStartPanel();
      submitAgentPrompt("continue");
    });
  }

  if (newSessionBtn) {
    newSessionBtn.addEventListener("click", () => {
      hideStartPanel();
      const newChatBtn = document.getElementById("newChatBtn");
      try { newChatBtn && newChatBtn.click(); } catch { /* ignore */ }
      submitAgentPrompt("Start");
    });
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
      renderStartPanel(data);
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
