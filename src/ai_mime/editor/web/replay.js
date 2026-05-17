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
    params: [],            // recursive input nodes from inputs.template.json
    lastRunValues: null,   // { [name]: value }
    skillDir: null,
    runActive: false,
    runTerminal: false,
    stdoutLines: [],
    stderrLines: [],
    lastExitCode: null,
    agentPromptSeeded: false,
    agentContextPrompt: "",
    agentHandoffBuffer: "",
    agentHandoffStarted: false,
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
      state.params = Object.entries(tpl).map(([name, raw]) => paramFromTemplateEntry(name, raw, [name]));
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
  function paramFromTemplateEntry(name, raw, path) {
    if (Array.isArray(raw)) {
      const sample = raw.length ? raw[0] : "";
      return {
        name,
        path,
        type: "array",
        required: true,
        default: raw,
        item: paramFromTemplateEntry(singularize(name), sample, [...path, 0]),
      };
    }
    if (raw && typeof raw === "object") {
      return {
        name,
        path,
        type: "object",
        required: true,
        default: raw,
        fields: Object.entries(raw).map(([childName, childRaw]) => (
          paramFromTemplateEntry(childName, childRaw, [...path, childName])
        )),
      };
    }

    const value = typeof raw === "string" ? raw : (raw == null ? "" : String(raw));
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
      path,
      type: inferType(typeof raw === "string" ? raw : raw, name),
      description,
      required,
      default: defaultVal,
    };
  }

  function singularize(name) {
    const value = String(name || "item");
    return value.endsWith("s") && value.length > 1 ? value.slice(0, -1) : value;
  }

  function inferType(raw, name) {
    if (typeof raw === "boolean") return "boolean";
    if (typeof raw === "number") return "number";
    const lc = String(name || "").toLowerCase();
    if (lc.endsWith("_path") || lc.endsWith("_dir") || lc.endsWith("_folder") || lc === "path") return "path";
    return "string";
  }

  function renderParamFields(values) {
    if (!state.params.length) {
      el.paramFields.innerHTML = `<div class="empty">This skill takes no parameters. Click Run to execute.</div>`;
      return;
    }
    const source = values || null;
    el.paramFields.innerHTML = state.params.map((p) => {
      const value = source ? getPathValue(source, p.path) : undefined;
      return renderParamNode(p, value, "root");
    }).join("");
  }

  function renderParamNode(p, value, scope) {
    if (p.type === "array") return renderArrayField(p, value);
    if (p.type === "object") return renderObjectField(p, value, scope);

    const path = pathToKey(p.path);
    const id = `param_${path.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
    const fieldValue = value !== undefined ? value : p.default;
    const fieldClass = scope === "object" ? "field nested-field" : "field full";
    const labelText = scope === "array-item" ? p.name : p.name;
    const dataAttrs = `data-path="${escapeHtml(path)}" data-type="${escapeHtml(p.type)}" data-required="${p.required ? "1" : "0"}"`;
      const required = p.required ? ` <span aria-hidden="true" title="required">*</span>` : "";
      const desc = p.description ? `<div class="desc">${escapeHtml(p.description)}</div>` : "";
    const defaultVal = fieldValue != null ? escapeHtml(String(fieldValue)) : "";
      if (p.type === "boolean" || p.type === "bool") {
        return `
        <div class="${fieldClass}" ${dataAttrs}>
          <label for="${id}">${escapeHtml(labelText)}${required}</label>
            <div class="checkbox-row">
            <input id="${id}" type="checkbox" ${fieldValue ? "checked" : ""} />
            <span>${escapeHtml(labelText)}</span>
            </div>
            ${desc}
          </div>
        `;
      }
      if (p.type === "number" || p.type === "integer") {
        return `
        <div class="${fieldClass}" ${dataAttrs}>
          <label for="${id}">${escapeHtml(labelText)}${required}</label>
            <input id="${id}" type="number" value="${defaultVal}" />
            ${desc}
          </div>
        `;
      }
      const placeholder = p.type === "path"
        ? "/absolute/path/to/…"
        : "";
      return `
      <div class="${fieldClass}" ${dataAttrs}>
        <label for="${id}">${escapeHtml(labelText)}${required}</label>
          <input id="${id}" type="text" value="${defaultVal}" placeholder="${escapeHtml(placeholder)}" spellcheck="false" />
          ${desc}
        </div>
      `;
  }

  function renderObjectField(p, value, scope) {
    const path = pathToKey(p.path);
    const title = scope === "array-item" ? "" : `<div class="group-title">${escapeHtml(p.name)}</div>`;
    const fields = (p.fields || []).map((child) => (
      renderParamNode(child, value ? value[child.name] : undefined, "object")
    )).join("");
    return `
      <fieldset class="field-group ${scope === "root" ? "full" : ""}" data-object-path="${escapeHtml(path)}">
        ${title}
        <div class="nested-fields">${fields}</div>
      </fieldset>
    `;
  }

  function renderArrayField(p, value) {
    const path = pathToKey(p.path);
    const values = Array.isArray(value)
      ? value
      : (Array.isArray(p.default) && p.default.length ? p.default : [defaultValueForNode(p.item)]);
    const itemName = p.item && p.item.name ? p.item.name : "item";
    const items = values.map((itemValue, index) => {
      const itemNode = cloneNodeWithPath(p.item, [...p.path, index]);
      return `
        <div class="array-item" data-array-item-index="${index}">
          <div class="array-item-head">
            <div class="array-item-title">${escapeHtml(itemName)} ${index + 1}</div>
            <button class="icon-btn danger" type="button" data-array-remove="${escapeHtml(path)}" data-array-index="${index}" ${values.length <= 1 ? "disabled" : ""} aria-label="Remove ${escapeHtml(itemName)} ${index + 1}">−</button>
          </div>
          ${renderParamNode(itemNode, itemValue, "array-item")}
        </div>
      `;
    }).join("");
    return `
      <fieldset class="field-array full" data-array-path="${escapeHtml(path)}">
        <div class="array-head">
          <div>
            <div class="group-title">${escapeHtml(p.name)}</div>
            <div class="desc">Add one entry for each ${escapeHtml(itemName)}.</div>
          </div>
          <button class="btn small" type="button" data-array-add="${escapeHtml(path)}">Add ${escapeHtml(itemName)}</button>
        </div>
        <div class="array-items">${items}</div>
      </fieldset>
    `;
  }

  function cloneNodeWithPath(node, path) {
    const copy = { ...node, path };
    if (node.fields) {
      copy.fields = node.fields.map((child) => cloneNodeWithPath(child, [...path, child.name]));
    }
    if (node.item) {
      copy.item = cloneNodeWithPath(node.item, [...path, 0]);
    }
    return copy;
  }

  function defaultValueForNode(node) {
    if (!node) return "";
    if (node.type === "array") return Array.isArray(node.default) ? [...node.default] : [];
    if (node.type === "object") {
      const out = {};
      for (const child of node.fields || []) out[child.name] = defaultValueForNode(child);
      return out;
    }
    if (node.type === "boolean" || node.type === "bool") return Boolean(node.default);
    if (node.type === "number" || node.type === "integer") return node.default === "" ? null : Number(node.default);
    return node.default == null ? "" : node.default;
  }

  function pathToKey(path) {
    return (path || []).map(String).join(".");
  }

  function getPathValue(obj, path) {
    let cur = obj;
    for (const part of path || []) {
      if (cur == null) return undefined;
      cur = cur[part];
    }
    return cur;
  }

  function setPathValue(obj, path, value) {
    let cur = obj;
    for (let i = 0; i < path.length - 1; i += 1) {
      const part = path[i];
      if (cur[part] == null) cur[part] = typeof path[i + 1] === "number" ? [] : {};
      cur = cur[part];
    }
    cur[path[path.length - 1]] = value;
  }

  function findParamNode(path) {
    const parts = Array.isArray(path) ? path : [];
    let nodes = state.params;
    let node = null;
    for (let i = 0; i < parts.length; i += 1) {
      const part = parts[i];
      if (typeof part === "number") {
        node = node && node.item ? node.item : null;
        if (!node) return null;
        continue;
      }
      node = (nodes || []).find((candidate) => candidate.name === part) || null;
      if (!node) return null;
      nodes = node.fields || [];
    }
    return node;
  }

  function keyToPath(key) {
    return String(key || "").split(".").filter(Boolean).map((part) => (
      /^\d+$/.test(part) ? Number(part) : part
    ));
  }

  function readParamValues() {
    const values = {};
    for (const field of el.paramFields.querySelectorAll(".field[data-path]")) {
      const input = field.querySelector("input");
      if (!input) continue;
      const path = keyToPath(field.dataset.path || "");
      const type = field.dataset.type || "string";
      if (type === "boolean" || type === "bool") {
        setPathValue(values, path, input.checked);
      } else if (type === "number" || type === "integer") {
        setPathValue(values, path, input.value === "" ? null : Number(input.value));
      } else {
        setPathValue(values, path, input.value);
      }
    }
    return values;
  }

  function validateForm() {
    let ok = true;
    for (const field of el.paramFields.querySelectorAll(".field[data-required='1']")) {
      const input = field.querySelector("input");
      if (!input || input.type === "checkbox") continue;
      const type = field.dataset.type || "string";
      const v = type === "number" || type === "integer"
        ? (input.value === "" ? null : Number(input.value))
        : input.value;
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
    state.stdoutLines = [];
    state.stderrLines = [];
    state.lastExitCode = null;
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
      state.stdoutLines.push(event.line || "");
      appendLog(event.line || "", "stdout");
    } else if (event.event === "stderr") {
      state.stderrLines.push(event.line || "");
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
        state.lastExitCode = event.exit_code == null ? null : event.exit_code;
        if (typeof event.stdout_log === "string") state.stdoutLines = event.stdout_log.split("\n");
        if (typeof event.stderr_log === "string") state.stderrLines = event.stderr_log.split("\n");
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
      exitCode: state.lastExitCode,
      params: state.lastRunValues || {},
      logsTail: tailLogs(120),
      stdoutTail: tailLines(state.stdoutLines, 120),
      stderrTail: tailLines(state.stderrLines, 120),
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

  function tailLines(lines, maxLines) {
    return (Array.isArray(lines) ? lines : []).slice(-maxLines).join("\n");
  }

  // ---------- form events ----------

  el.paramFields.addEventListener("click", (e) => {
    const addBtn = e.target.closest("[data-array-add]");
    const removeBtn = e.target.closest("[data-array-remove]");
    if (!addBtn && !removeBtn) return;
    e.preventDefault();

    const key = (addBtn || removeBtn).dataset.arrayAdd || (addBtn || removeBtn).dataset.arrayRemove;
    const path = keyToPath(key);
    const node = findParamNode(path);
    const values = readParamValues();
    let arr = getPathValue(values, path);
    if (!Array.isArray(arr)) {
      arr = [];
      setPathValue(values, path, arr);
    }

    if (addBtn) {
      arr.push(defaultValueForNode(node && node.item));
    } else {
      const index = Number(removeBtn.dataset.arrayIndex);
      if (arr.length > 1 && Number.isInteger(index)) arr.splice(index, 1);
    }

    renderParamFields(values);
    if (state.mode === "idle" || state.mode === "agent_typing") {
      setMode("script_typing");
    }
    validateForm();
  });

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
    if (el.messageInput) {
      el.messageInput.disabled = false;
      el.messageInput.readOnly = false;
    }
    // Focus the message input once the sheet is up.
    setTimeout(() => { try { el.messageInput.focus(); } catch {} }, 60);
  }
  function closeAgentOverlay() {
    if (el.agentOverlay.hidden) return;
    el.agentOverlay.hidden = true;
  }

  el.agentPill.addEventListener("click", () => {
    prepareReplayAgentContext();
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

  function buildReplayAgentPrompt(values) {
    const params = values || {};
    return [
      `Learn from the skill at ${state.skillDir || "(unknown skill dir)"} and run this replay task.`,
      ``,
      `Use these inputs:`,
      "```json",
      JSON.stringify(params, null, 2),
      "```",
      ``,
      `First validate or normalize the inputs. Prefer running ./run.sh with an inputs JSON file because it is cheap, end-to-end, and emits rich progress logs. Use the logs to report progress and results.`,
      ``,
      `If the original script itself fails because the skill appears stale or broken, do not heal it in replay mode. Say it needs skill-build healing and summarize the error and relevant logs.`,
      `End that failure response with the AI_MIME_REPLAY_HANDOFF_TO_SKILL_BUILD marker so Replay can switch to build-skill automatically.`,
    ].join("\n");
  }

  function prepareReplayAgentContext() {
    if (state.agentPromptSeeded) return;
    const values = state.lastRunValues || (validateForm() ? readParamValues() : null);
    if (!values) return;
    state.agentContextPrompt = buildReplayAgentPrompt(values);
    try {
      sessionStorage.setItem(`replay:agent-context:${taskId}`, state.agentContextPrompt);
    } catch { /* sessionStorage may be unavailable */ }
    state.agentPromptSeeded = true;
  }

  el.messageInput.addEventListener("focus", () => {
    setMode("agent_typing");
  });

  window.addEventListener("agent-stream-event", (e) => {
    const event = e && e.detail ? e.detail : null;
    if (!event || event.event !== "text" || typeof event.text !== "string") return;
    maybeHandleReplayAgentHandoff(event.text);
  });

  function maybeHandleReplayAgentHandoff(chunk) {
    if (state.agentHandoffStarted) return;
    state.agentHandoffBuffer = (state.agentHandoffBuffer + chunk).slice(-12000);
    const marker = "AI_MIME_REPLAY_HANDOFF_TO_SKILL_BUILD";
    const markerIdx = state.agentHandoffBuffer.indexOf(marker);
    if (markerIdx === -1) return;
    const afterMarker = state.agentHandoffBuffer.slice(markerIdx + marker.length);
    const jsonMatch = afterMarker.match(/\{[\s\S]*\}/);
    if (!jsonMatch) return;
    let handoff = {};
    try {
      handoff = JSON.parse(jsonMatch[0]);
    } catch {
      return;
    }
    state.agentHandoffStarted = true;
    const payload = {
      taskId,
      error: handoff.error || "Replay agent reported that the original script failed.",
      exitCode: handoff.exitCode == null ? null : handoff.exitCode,
      params: state.lastRunValues || (validateForm() ? readParamValues() : {}),
      logsTail: handoff.logsTail || tailLogs(120),
      stdoutTail: handoff.stdoutTail || "",
      stderrTail: handoff.stderrTail || "",
      skillDir: state.skillDir,
      at: new Date().toISOString(),
      source: "replay_agent",
    };
    try {
      sessionStorage.setItem(`replay:handover:${taskId}`, JSON.stringify(payload));
    } catch { /* sessionStorage may be unavailable */ }
    window.location.assign(`/skill-build/${encodeURIComponent(taskId)}`);
  }

  bootstrap();
})();
