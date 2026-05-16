(() => {
  const shell = document.querySelector(".replay-shell");
  if (!shell) return;
  const taskId = shell.dataset.taskId || "";

  const el = {
    shell,
    taskName: document.getElementById("taskName"),
    statusPill: document.getElementById("statusPill"),
    skillDir: document.getElementById("skillDir"),
    skillDirInline: document.getElementById("skillDirInline"),
    scriptCard: document.getElementById("scriptCard"),
    expandFormBtn: document.getElementById("expandFormBtn"),
    scriptForm: document.getElementById("scriptForm"),
    paramFields: document.getElementById("paramFields"),
    runBtn: document.getElementById("runBtn"),
    scriptError: document.getElementById("scriptError"),
    scriptSummary: document.getElementById("scriptSummary"),
    summaryParams: document.getElementById("summaryParams"),
    editParamsBtn: document.getElementById("editParamsBtn"),
    rerunBtn: document.getElementById("rerunBtn"),
    newRunBtn: document.getElementById("newRunBtn"),
    runOutput: document.getElementById("runOutput"),
    runStatus: document.getElementById("runStatus"),
    logsPre: document.getElementById("logsPre"),
    outputsPanel: document.getElementById("outputsPanel"),
    failureBanner: document.getElementById("failureBanner"),
    agentPill: document.getElementById("agentPill"),
    agentOverlay: document.getElementById("agentOverlay"),
    dockCloseBtn: document.getElementById("dockCloseBtn"),
    messages: document.getElementById("messages"),
    chatForm: document.getElementById("chatForm"),
    messageInput: document.getElementById("messageInput"),
    sendBtn: document.getElementById("sendBtn"),
    tabs: document.querySelectorAll(".tab"),
    tabPanels: document.querySelectorAll(".tab-panel"),
  };

  const state = {
    mode: "idle",
    params: [],            // [{name, type, description, required, default}]
    lastRunValues: null,   // { [name]: value }
    skillDir: null,
    runActive: false,
    runTerminal: false,
  };

  // ---------- helpers ----------

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  async function request(path, options) {
    const res = await fetch(path, options);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { /* keep raw */ }
    if (!res.ok) {
      const detail = data && data.detail ? data.detail : text || `HTTP ${res.status}`;
      throw new Error(detail);
    }
    return data;
  }

  // ---------- mode state machine ----------

  function setMode(next) {
    if (state.mode === next) return;
    // Disallow exiting a terminal failure state via focus changes —
    // we navigate away to skill-build automatically.
    const prev = state.mode;
    state.mode = next;
    shell.dataset.mode = next;

    if (next === "running_script" || next === "script_done" || next === "script_failed" || next === "agent_active") {
      el.scriptCard.classList.add("is-compact");
    } else {
      el.scriptCard.classList.remove("is-compact");
    }

    if (next === "agent_typing" || next === "agent_active") {
      openAgentOverlay();
    } else {
      closeAgentOverlay();
    }

    // log transition for debug
    // eslint-disable-next-line no-console
    console.debug("[replay] mode", prev, "->", next);
  }

  // ---------- bootstrap ----------

  async function bootstrap() {
    // Header / task row — independent of schema.
    let tasksResp = null;
    try {
      tasksResp = await request("/api/tasks");
    } catch (e) {
      console.warn("[replay] /api/tasks failed:", e);
    }
    const row = tasksResp && Array.isArray(tasksResp.tasks)
      ? tasksResp.tasks.find((t) => t.id === taskId)
      : null;
    if (row) {
      el.taskName.textContent = row.display_name || row.id;
      if (row.status) {
        el.statusPill.hidden = false;
        el.statusPill.textContent = row.status;
        el.statusPill.dataset.state = row.status === "ready" ? "ready" : "";
      }
      state.skillDir = row.skill_dir || null;
    } else {
      el.taskName.textContent = taskId;
    }
    if (state.skillDir) {
      el.skillDir.textContent = state.skillDir;
      if (el.skillDirInline) el.skillDirInline.textContent = state.skillDir;
    }

    // Skill inputs template — the canonical place for replay inputs is
    // <skill_dir>/inputs/inputs.template.json. Best-effort: render the page
    // anyway if it fails so the user can still use the agent.
    try {
      const resp = await request(`/api/tasks/${encodeURIComponent(taskId)}/skill/inputs-template`);
      if (resp && resp.skill_dir) {
        state.skillDir = resp.skill_dir;
        el.skillDir.textContent = resp.skill_dir;
        if (el.skillDirInline) el.skillDirInline.textContent = resp.skill_dir;
      }
      const tpl = (resp && resp.template) || {};
      state.params = Object.entries(tpl).map(([name, raw]) => paramFromTemplateEntry(name, raw));
      renderParamFields();
      validateForm();
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      el.paramFields.innerHTML = `
        <div class="empty">
          Could not load <code>inputs/inputs.template.json</code>: ${escapeHtml(msg)}.
          <div style="margin-top:6px">You can still run the agent below.</div>
        </div>`;
      state.params = [];
      el.runBtn.disabled = true;
    }
  }

  // inputs.template.json values look like:
  //   "<FILL IN: absolute path to a folder containing ...>"
  // or plain default strings. Parse the placeholder hint into `description`
  // and treat anything that doesn't look like a <FILL IN> sentinel as a
  // pre-filled default value.
  function paramFromTemplateEntry(name, raw) {
    const value = typeof raw === "string" ? raw : (raw == null ? "" : JSON.stringify(raw));
    let description = "";
    let defaultVal = "";
    let required = true;
    const m = value.match(/^<\s*FILL IN\s*:\s*([\s\S]*?)\s*>\s*$/i);
    if (m) {
      description = m[1].trim();
    } else if (value === "<FILL IN>" || /^<\s*FILL IN\s*>$/i.test(value)) {
      description = "";
    } else {
      defaultVal = value;
      required = false;
    }
    return {
      name,
      type: inferType(typeof raw === "string" ? raw : raw, name),
      description,
      required,
      default: defaultVal,
    };
  }

  function inferType(raw, name) {
    if (typeof raw === "boolean") return "boolean";
    if (typeof raw === "number") return "number";
    const lc = String(name || "").toLowerCase();
    if (lc.endsWith("_path") || lc.endsWith("_dir") || lc.endsWith("_folder") || lc === "path") return "path";
    return "string";
  }

  function renderParamFields() {
    if (!state.params.length) {
      el.paramFields.innerHTML = `<div class="empty">This skill takes no parameters. Click Run to execute.</div>`;
      return;
    }
    el.paramFields.innerHTML = state.params.map((p) => {
      const id = `param_${p.name}`;
      const required = p.required ? ` <span aria-hidden="true" title="required">*</span>` : "";
      const desc = p.description ? `<div class="desc">${escapeHtml(p.description)}</div>` : "";
      const defaultVal = p.default != null ? escapeHtml(String(p.default)) : "";
      if (p.type === "boolean" || p.type === "bool") {
        return `
          <div class="field full" data-name="${escapeHtml(p.name)}" data-type="bool">
            <label for="${id}">${escapeHtml(p.name)}${required}</label>
            <div class="checkbox-row">
              <input id="${id}" type="checkbox" ${p.default ? "checked" : ""} />
              <span>${escapeHtml(p.name)}</span>
            </div>
            ${desc}
          </div>
        `;
      }
      if (p.type === "number" || p.type === "integer") {
        return `
          <div class="field" data-name="${escapeHtml(p.name)}" data-type="number">
            <label for="${id}">${escapeHtml(p.name)}${required}</label>
            <input id="${id}" type="number" value="${defaultVal}" />
            ${desc}
          </div>
        `;
      }
      const placeholder = p.type === "path"
        ? "/absolute/path/to/…"
        : "";
      return `
        <div class="field full" data-name="${escapeHtml(p.name)}" data-type="${escapeHtml(p.type)}">
          <label for="${id}">${escapeHtml(p.name)}${required}</label>
          <input id="${id}" type="text" value="${defaultVal}" placeholder="${escapeHtml(placeholder)}" spellcheck="false" />
          ${desc}
        </div>
      `;
    }).join("");
  }

  function readParamValues() {
    const values = {};
    for (const p of state.params) {
      const field = el.paramFields.querySelector(`.field[data-name="${CSS.escape(p.name)}"]`);
      if (!field) continue;
      const input = field.querySelector("input");
      if (!input) continue;
      if (p.type === "boolean" || p.type === "bool") {
        values[p.name] = input.checked;
      } else if (p.type === "number" || p.type === "integer") {
        values[p.name] = input.value === "" ? null : Number(input.value);
      } else {
        values[p.name] = input.value;
      }
    }
    return values;
  }

  function validateForm() {
    const values = readParamValues();
    let ok = true;
    for (const p of state.params) {
      if (!p.required) continue;
      const v = values[p.name];
      if (v === "" || v == null || (typeof v === "number" && Number.isNaN(v))) ok = false;
    }
    el.runBtn.disabled = !ok || state.runActive;
    return ok;
  }

  function formatSummary(values) {
    const pieces = Object.entries(values).map(([k, v]) => {
      if (v === "" || v == null) return `${k}=∅`;
      const s = typeof v === "string" ? v : JSON.stringify(v);
      const short = s.length > 32 ? s.slice(0, 30) + "…" : s;
      return `${k}=${short}`;
    });
    return pieces.join(" · ") || "(no params)";
  }

  // ---------- tabs ----------

  function selectTab(name) {
    el.tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    el.tabPanels.forEach((p) => p.classList.toggle("active", p.dataset.tab === name));
  }

  el.tabs.forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab)));

  // ---------- script run (STUB) ----------

  function appendLog(line, kind) {
    const span = document.createElement("span");
    if (kind === "stderr") span.className = "log-stderr";
    span.textContent = (line || "") + "\n";
    el.logsPre.appendChild(span);
    el.logsPre.scrollTop = el.logsPre.scrollHeight;
  }

  function appendOutput(key, value) {
    if (el.outputsPanel.querySelector(".empty")) el.outputsPanel.innerHTML = "";
    const row = document.createElement("div");
    row.className = "output-row";
    row.innerHTML = `
      <div class="output-key">${escapeHtml(key)}</div>
      <div class="output-value">${escapeHtml(typeof value === "string" ? value : JSON.stringify(value, null, 2))}</div>
    `;
    el.outputsPanel.appendChild(row);
  }

  function resetOutputs() {
    el.logsPre.innerHTML = "";
    el.outputsPanel.innerHTML = `<div class="empty">No outputs yet.</div>`;
    el.failureBanner.hidden = true;
  }

  async function startScriptRun(values) {
    state.lastRunValues = values;
    state.runActive = true;
    state.runTerminal = false;
    resetOutputs();
    el.runOutput.hidden = false;
    selectTab("logs");
    el.runStatus.textContent = "Running…";
    el.runStatus.dataset.state = "running";
    el.summaryParams.textContent = formatSummary(values);
    el.scriptSummary.hidden = false;
    setMode("running_script");

    try {
      await streamScriptRun(values);
    } catch (err) {
      if (state.runTerminal) return;
      handleScriptFailure(err && err.message ? err.message : String(err || "Unknown error"));
    }
  }

  async function streamScriptRun(values) {
    const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/skill/run/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params: values }),
    });
    if (!response.ok) {
      const text = await response.text();
      let detail = text;
      try {
        const data = JSON.parse(text);
        detail = data && data.detail ? data.detail : text;
      } catch { /* keep raw */ }
      throw new Error(detail || `HTTP ${response.status}`);
    }
    if (!response.body) throw new Error("Run stream is unavailable in this browser.");
    await consumeRunStream(response);
  }

  async function consumeRunStream(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        for (const line of raw.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          try {
            handleRunEvent(JSON.parse(payload));
          } catch {
            appendLog(`[replay] ignored malformed stream event: ${payload}`, "stderr");
          }
        }
      }
    }
    if (!state.runTerminal) {
      throw new Error("Run stream ended before reporting status.");
    }
  }

  function handleRunEvent(event) {
    if (!event || typeof event !== "object") return;
    if (state.runTerminal && event.event !== "stdout" && event.event !== "stderr") return;
    if (event.event === "started") {
      appendLog(`$ cd ${event.skill_dir || state.skillDir || "."} && ${event.command || "./run.sh"}`);
    } else if (event.event === "stdout") {
      appendLog(event.line || "", "stdout");
    } else if (event.event === "stderr") {
      appendLog(event.line || "", "stderr");
    } else if (event.event === "output") {
      appendOutput(event.key || "output", event.value);
    } else if (event.event === "done") {
      if (
        event.outputs &&
        typeof event.outputs === "object" &&
        Object.keys(event.outputs).length &&
        el.outputsPanel.querySelector(".empty")
      ) {
        appendOutput("final", event.outputs);
      }
      if (event.success) {
        finishScriptRun("done");
      } else {
        const code = event.exit_code == null ? "unknown" : event.exit_code;
        handleScriptFailure(`run.sh exited with code ${code}`);
      }
    } else if (event.event === "error") {
      handleScriptFailure(event.message || "Run failed");
    }
  }

  function finishScriptRun(kind) {
    if (state.runTerminal) return;
    state.runTerminal = true;
    state.runActive = false;
    if (kind === "done") {
      el.runStatus.textContent = "Succeeded";
      el.runStatus.dataset.state = "done";
      setMode("script_done");
      selectTab("outputs");
    }
    validateForm();
  }

  function handleScriptFailure(message) {
    if (state.runTerminal) return;
    state.runTerminal = true;
    state.runActive = false;
    el.runStatus.textContent = "Failed";
    el.runStatus.dataset.state = "failed";
    el.failureBanner.hidden = false;
    setMode("script_failed");

    // Auto-handover to skill-build for healing.
    const payload = {
      taskId,
      error: message,
      params: state.lastRunValues || {},
      logsTail: tailLogs(120),
      skillDir: state.skillDir,
      at: new Date().toISOString(),
    };
    try {
      sessionStorage.setItem(`replay:handover:${taskId}`, JSON.stringify(payload));
    } catch { /* sessionStorage may be unavailable */ }

    // Small delay so the user sees what happened before we navigate.
    setTimeout(() => {
      window.location.assign(`/skill-build/${encodeURIComponent(taskId)}`);
    }, 1400);
  }

  function tailLogs(maxLines) {
    const text = el.logsPre.innerText || "";
    const lines = text.split("\n");
    return lines.slice(-maxLines).join("\n");
  }

  // ---------- form events ----------

  el.scriptForm.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!validateForm()) return;
    startScriptRun(readParamValues());
  });

  el.paramFields.addEventListener("input", () => {
    if (state.mode === "idle" || state.mode === "agent_typing") {
      setMode("script_typing");
    }
    validateForm();
  });
  el.paramFields.addEventListener("focusin", () => {
    if (state.mode === "idle" || state.mode === "agent_typing") {
      setMode("script_typing");
    }
  });
  el.paramFields.addEventListener("focusout", (e) => {
    // If no field within paramFields holds focus and we're typing, return to idle.
    setTimeout(() => {
      if (state.mode !== "script_typing") return;
      if (!el.paramFields.contains(document.activeElement)) {
        setMode("idle");
      }
    }, 0);
  });

  el.rerunBtn.addEventListener("click", () => {
    if (state.lastRunValues) startScriptRun(state.lastRunValues);
  });
  el.editParamsBtn.addEventListener("click", () => {
    el.scriptCard.classList.remove("is-compact");
    el.scriptSummary.hidden = true;
  });
  el.expandFormBtn.addEventListener("click", () => {
    el.scriptCard.classList.remove("is-compact");
  });
  el.newRunBtn.addEventListener("click", () => {
    if (state.runActive && !confirm("A run is in flight. Discard it?")) return;
    state.runActive = false;
    state.runTerminal = false;
    state.lastRunValues = null;
    el.runOutput.hidden = true;
    el.scriptSummary.hidden = true;
    el.scriptCard.classList.remove("is-compact");
    resetOutputs();
    setMode("idle");
    validateForm();
  });

  // ---------- agent dock ----------

  function openAgentOverlay() {
    if (!el.agentOverlay.hidden) return;
    el.agentOverlay.hidden = false;
    // Focus the message input once the sheet is up.
    setTimeout(() => { try { el.messageInput.focus(); } catch {} }, 60);
  }
  function closeAgentOverlay() {
    if (el.agentOverlay.hidden) return;
    el.agentOverlay.hidden = true;
  }

  el.agentPill.addEventListener("click", () => {
    setMode(state.runActive ? "agent_active" : "agent_typing");
  });
  el.dockCloseBtn.addEventListener("click", () => {
    // Collapse back to the previous "resting" state.
    if (state.lastRunValues) {
      setMode("script_done");
    } else {
      setMode("idle");
    }
  });

  // Click on overlay backdrop closes
  el.agentOverlay.addEventListener("mousedown", (e) => {
    if (e.target === el.agentOverlay) el.dockCloseBtn.click();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !el.agentOverlay.hidden) el.dockCloseBtn.click();
  });

  // STUB chat submit — until the variation-agent transport is wired,
  // we just echo the message into the transcript so the UX can be tested.
  el.chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = (el.messageInput.value || "").trim();
    if (!text) return;
    if (el.messages.querySelector(".empty-state")) el.messages.innerHTML = "";

    const userMsg = document.createElement("div");
    userMsg.className = "message message--user";
    userMsg.style.padding = "10px 14px";
    userMsg.innerHTML = `<div style="font-weight:600;margin-bottom:4px">You</div>${escapeHtml(text)}`;
    el.messages.appendChild(userMsg);

    const stub = document.createElement("div");
    stub.className = "message message--agent";
    stub.style.padding = "10px 14px";
    stub.style.color = "var(--muted, #7a746d)";
    stub.textContent = "TODO(agent-runner): the variation agent is not wired yet.";
    el.messages.appendChild(stub);

    el.messageInput.value = "";
    el.messages.scrollTop = el.messages.scrollHeight;
    setMode("agent_active");
  });

  el.messageInput.addEventListener("focus", () => {
    setMode("agent_typing");
  });

  bootstrap();
})();
