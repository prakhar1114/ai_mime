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
    killTaskBtn: document.getElementById("killTaskBtn"),
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
    olderRunsCard: document.getElementById("olderRunsCard"),
    runsList: document.getElementById("runsList"),
    logsTabBtn: document.getElementById("logsTabBtn"),
    toggleOlderRunsBtn: document.getElementById("toggleOlderRunsBtn"),
    olderRunDetailCard: document.getElementById("olderRunDetailCard"),
    olderLogsTabBtn: document.getElementById("olderLogsTabBtn"),
    olderOutputsTabBtn: document.getElementById("olderOutputsTabBtn"),
    olderRunStatus: document.getElementById("olderRunStatus"),
    olderLogsPre: document.getElementById("olderLogsPre"),
    olderOutputsPanel: document.getElementById("olderOutputsPanel"),
    olderFailureBanner: document.getElementById("olderFailureBanner"),
    closeOlderDetailBtn: document.getElementById("closeOlderDetailBtn"),
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
    logLines: [],
    lastExitCode: null,
    agentPromptSeeded: false,
    agentContextPrompt: "",
    agentFallbackStarted: false,
    killedByUser: false,
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
      const isCompileError = msg.includes("compile_workflow_schema") || msg.includes("schema_compiler") || msg.includes("Traceback");
      const displayMsg = isCompileError 
        ? "The workflow schema failed to compile. Please check the reflection logs." 
        : escapeHtml(msg);
      el.paramFields.innerHTML = `
        <div class="empty">
          Could not load <code>inputs/inputs.template.json</code>: ${displayMsg}
          <div style="margin-top:6px">You can still run the agent below.</div>
        </div>`;
      state.params = [];
      el.runBtn.disabled = true;
    }

    try {
      await loadOlderRuns();
    } catch (err) {
      console.warn("[replay] loadOlderRuns failed:", err);
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
      const item = paramFromTemplateEntry(singularize(name), sample, [...path, 0]);
      return {
        name,
        path,
        type: "array",
        required: true,
        default: raw.length ? raw.map((itemRaw, index) => (
          defaultValueForNode(paramFromTemplateEntry(singularize(name), itemRaw, [...path, index]))
        )) : [defaultValueForNode(item)],
        item,
      };
    }
    if (raw && typeof raw === "object") {
      const fields = Object.entries(raw).map(([childName, childRaw]) => (
        paramFromTemplateEntry(childName, childRaw, [...path, childName])
      ));
      const defaults = {};
      for (const child of fields) defaults[child.name] = defaultValueForNode(child);
      return {
        name,
        path,
        type: "object",
        required: true,
        default: defaults,
        fields,
      };
    }

    const value = typeof raw === "string" ? raw : (raw == null ? "" : String(raw));
    let description = "";
    let defaultVal = "";
    let required = true;
    const m = fillInHintMatch(value);
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

  function fillInHintMatch(value) {
    if (typeof value !== "string") return null;
    return value.match(/^<\s*FILL IN\s*:\s*([\s\S]*?)\s*>\s*$/i);
  }

  function isFillInHintValue(value) {
    if (typeof value !== "string") return false;
    return Boolean(fillInHintMatch(value) || /^<\s*FILL IN\s*>\s*$/i.test(value));
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
        setPathValue(values, path, isFillInHintValue(input.value) ? "" : input.value);
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
      if (v === "" || v == null || isFillInHintValue(v) || (typeof v === "number" && Number.isNaN(v))) ok = false;
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

  function selectTab(name, container = document) {
    const tabs = container.querySelectorAll(".tab");
    const panels = container.querySelectorAll(".tab-panel");
    tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    panels.forEach((p) => p.classList.toggle("active", p.dataset.tab === name));
  }

  el.runOutput.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => selectTab(t.dataset.tab, el.runOutput));
  });

  el.olderRunDetailCard.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => selectTab(t.dataset.tab, el.olderRunDetailCard));
  });

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
    state.logLines = [];
    state.lastExitCode = null;
  }

  async function startScriptRun(values) {
    state.lastRunValues = values;
    state.runActive = true;
    state.runTerminal = false;
    state.killedByUser = false;
    resetOutputs();
    el.runOutput.hidden = false;
    if (el.logsTabBtn) el.logsTabBtn.style.display = "";
    selectTab("logs", el.runOutput);
    el.runStatus.textContent = "Running…";
    el.runStatus.dataset.state = "running";
    if (el.killTaskBtn) el.killTaskBtn.hidden = false;
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
      const startCmd = `$ cd ${event.skill_dir || state.skillDir || "."} && ${event.command || "./run.sh"}`;
      appendLog(startCmd);
      state.logLines.push(startCmd);
    } else if (event.event === "stdout") {
      state.stdoutLines.push(event.line || "");
      state.logLines.push(event.line || "");
      appendLog(event.line || "", "stdout");
    } else if (event.event === "stderr") {
      state.stderrLines.push(event.line || "");
      state.logLines.push(`[stderr] ${event.line || ""}`);
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
        if (typeof event.combined_log === "string") state.logLines = event.combined_log.split("\n");
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
    if (el.killTaskBtn) el.killTaskBtn.hidden = true;
    if (kind === "done") {
      el.runStatus.textContent = "Succeeded";
      el.runStatus.dataset.state = "done";
      setMode("script_done");
      selectTab("outputs");
    }
    validateForm();
    loadOlderRuns();
  }

  function handleScriptFailure(message) {
    if (state.runTerminal) return;
    state.runTerminal = true;
    state.runActive = false;
    if (el.killTaskBtn) el.killTaskBtn.hidden = true;
    el.runStatus.textContent = state.killedByUser ? "Killed" : "Failed";
    el.runStatus.dataset.state = "failed";
    el.failureBanner.hidden = state.killedByUser;
    setMode("script_failed");
    loadOlderRuns();

    if (state.killedByUser) return;

    const payload = {
      taskId,
      error: message,
      exitCode: state.lastExitCode,
      params: state.lastRunValues || {},
      logsTail: tailLines(state.logLines, 120),
      stdoutTail: tailLines(state.stdoutLines, 60),
      stderrTail: tailLines(state.stderrLines, 60),
      skillDir: state.skillDir,
      at: new Date().toISOString(),
    };
    startUiAgentFallback(payload);
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
  el.paramFields.addEventListener("focusin", (e) => {
    const input = e.target && e.target.closest ? e.target.closest("input") : null;
    if (!input || input.type === "checkbox") return;
    if (!isFillInHintValue(input.value)) return;
    input.value = "";
    validateForm();
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
    if (el.killTaskBtn) el.killTaskBtn.hidden = true;
    el.scriptCard.classList.remove("is-compact");
    resetOutputs();
    setMode("idle");
    validateForm();
  });

  if (el.killTaskBtn) {
    el.killTaskBtn.addEventListener("click", async () => {
      try {
        state.killedByUser = true;
        const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/skill/kill`, {
          method: "POST"
        });
        if (!response.ok) {
          console.warn("[replay] failed to kill task", await response.text());
        }
      } catch (err) {
        console.warn("[replay] failed to kill task", err);
      }
    });
  }

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
      `If ./run.sh fails, read the complete skill package including references/fallback_plan.md, use the UI agent ("$AI_MIME_UI_AGENT_CMD") for unknown UI-only parts, and decide from the logs and skill context how to complete the task.`,
    ].join("\n");
  }

  function buildUiAgentFallbackPrompt(payload) {
    const params = payload.params || {};
    const logs = payload.logsTail || "(no logs captured)";
    return [
      `The deterministic replay script failed. Stay in replay execution mode and complete this task now.`,
      ``,
      `Skill directory: ${payload.skillDir || "(unknown skill dir)"}`,
      ``,
      `Inputs used:`,
      "```json",
      JSON.stringify(params, null, 2),
      "```",
      ``,
      `Exit code: ${payload.exitCode == null ? "(unknown)" : payload.exitCode}`,
      `Error: ${payload.error || "(no error message)"}`,
      ``,
      `Recent logs:`,
      "```",
      logs,
      "```",
      ``,
      `Read the complete skill package before deciding what to do: SKILL.md, run.sh, scripts/run.py, inputs/inputs.example.json, inputs/inputs.template.json, all files under references/, and especially references/fallback_plan.md.`,
      `First triage the failure before editing anything: decide whether this is an environment/user-state issue, input issue, transient UI issue, or actual skill defect. Closed tabs, missing windows, changed focus, logged-out browser state, interrupted app state, and one-off UI disruption should be recovered from without repairing the skill.`,
      `Use the UI agent ("$AI_MIME_UI_AGENT_CMD") for unknown UI-only parts. Restore or continue the expected UI state first and complete the task from the fallback plan and logs when possible.`,
      `Only rewrite run.sh, scripts/run.py, or other skill files if the logs/script show a real skill defect that would likely fail again from a normal starting state. Prioritize completing this run and reporting the final result.`,
    ].join("\n");
  }

  function submitAgentPrompt(prompt) {
    if (!el.messageInput || !el.chatForm) return false;
    el.messageInput.value = prompt;
    el.messageInput.dispatchEvent(new Event("input", { bubbles: true }));
    setTimeout(() => {
      if (typeof el.chatForm.requestSubmit === "function") {
        el.chatForm.requestSubmit();
      } else {
        el.chatForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      }
    }, 80);
    return true;
  }

  function startUiAgentFallback(payload) {
    if (state.agentFallbackStarted) return;
    state.agentFallbackStarted = true;
    state.agentPromptSeeded = true;
    state.agentContextPrompt = buildUiAgentFallbackPrompt(payload);
    try {
      sessionStorage.setItem(`replay:agent-context:${taskId}`, state.agentContextPrompt);
    } catch { /* sessionStorage may be unavailable */ }

    if (el.failureBanner) {
      el.failureBanner.innerHTML = `
        <div class="failure-text">
          Script failed. Handing off to the UI agent to complete the task…
        </div>
        <div class="failure-spinner" aria-hidden="true"></div>
      `;
    }
    setMode("agent_active");
    if (window.AgentChat && typeof window.AgentChat.newChat === "function") {
      window.AgentChat.newChat();
    }

    function trySubmit() {
      if (submitAgentPrompt(state.agentContextPrompt)) return;
      setTimeout(trySubmit, 120);
    }
    setTimeout(trySubmit, 0);
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

  async function loadOlderRuns() {
    try {
      const resp = await request(`/api/tasks/${encodeURIComponent(taskId)}/runs`);
      if (resp && Array.isArray(resp.runs) && resp.runs.length > 0) {
        if (el.toggleOlderRunsBtn) {
          el.toggleOlderRunsBtn.style.display = "";
          el.toggleOlderRunsBtn.hidden = false;
        }
        el.runsList.innerHTML = resp.runs.map((r) => {
          const statusClass = r.status === "success" ? "success" : (r.status === "failed" ? "failed" : "running");
          const dateStr = r.started ? new Date(r.started).toLocaleString() : r.run_id;
          return `
            <div class="run-item" data-run-id="${escapeHtml(r.run_id)}">
              <div class="run-item-left">
                <span class="run-badge ${statusClass}">${escapeHtml(r.status)}</span>
                <span class="run-item-id">${escapeHtml(r.run_id)}</span>
              </div>
              <div class="run-item-right">
                <span class="run-item-date">${escapeHtml(dateStr)}</span>
                <button class="btn small primary retry-run-btn" data-run-id="${escapeHtml(r.run_id)}" style="padding: 2px 10px; font-size: 11.5px; height: 26px; min-height: 26px; font-weight: 600; margin-left: 12px; border-radius: 6px; cursor: pointer;">Retry ↻</button>
              </div>
            </div>
          `;
        }).join("");
      } else {
        if (el.toggleOlderRunsBtn) {
          el.toggleOlderRunsBtn.style.display = "none";
          el.toggleOlderRunsBtn.hidden = true;
        }
        el.olderRunsCard.hidden = true;
      }
    } catch (e) {
      console.warn("[replay] failed to load older runs:", e);
      if (el.toggleOlderRunsBtn) {
        el.toggleOlderRunsBtn.style.display = "none";
        el.toggleOlderRunsBtn.hidden = true;
      }
      el.olderRunsCard.hidden = true;
    }
  }

  function parseRunMarkdown(md) {
    const res = {
      status: "unknown",
      started: "",
      duration: "",
      exitCode: null,
      command: "",
      input: null,
      output: null,
      error: "",
      logs: "",
      stdout: "",
      stderr: ""
    };

    const statusMatch = md.match(/-\s*Status:\s*([^\n]+)/i);
    if (statusMatch) res.status = statusMatch[1].trim();

    const startedMatch = md.match(/-\s*Started:\s*([^\n]+)/i);
    if (startedMatch) res.started = startedMatch[1].trim();

    const durationMatch = md.match(/-\s*Duration:\s*([^\n]+)/i);
    if (durationMatch) res.duration = durationMatch[1].trim();

    const exitMatch = md.match(/-\s*Exit code:\s*([^\n]+)/i);
    if (exitMatch) res.exitCode = parseInt(exitMatch[1].trim(), 10);

    const extractSection = (title) => {
      const escapedTitle = title.replace(/[-\/\\^$*+?.()|[\]{}]/g, '\\$&');
      const regex = new RegExp(`##\\s*${escapedTitle}\\s*\\n([\\s\\S]*?)(?:\\n##|$)`, 'i');
      const match = md.match(regex);
      if (!match) return "";
      let content = match[1].trim();
      if (content.startsWith("```")) {
        content = content.replace(/^```[a-zA-Z]*\n/, "").replace(/\n```$/, "");
      }
      return content.trim();
    };

    res.command = extractSection("Command Executed");
    res.error = extractSection("Error");
    res.logs = extractSection("Logs");
    res.stdout = extractSection("Standard Output");
    res.stderr = extractSection("Standard Error");

    const inputStr = extractSection("Input");
    if (inputStr) {
      try { res.input = JSON.parse(inputStr); } catch (e) { res.input = inputStr; }
    }

    const outputStr = extractSection("Output");
    if (outputStr) {
      try { res.output = JSON.parse(outputStr); } catch (e) { res.output = outputStr; }
    }

    return res;
  }

  function appendOlderLog(line, kind) {
    const span = document.createElement("span");
    if (kind === "stderr") span.className = "log-stderr";
    span.textContent = (line || "") + "\n";
    el.olderLogsPre.appendChild(span);
    el.olderLogsPre.scrollTop = el.olderLogsPre.scrollHeight;
  }

  function appendOlderOutput(key, value) {
    if (el.olderOutputsPanel.querySelector(".empty")) el.olderOutputsPanel.innerHTML = "";
    const row = document.createElement("div");
    row.className = "output-row";
    row.innerHTML = `
      <div class="output-key">${escapeHtml(key)}</div>
      <div class="output-value">${escapeHtml(typeof value === "string" ? value : JSON.stringify(value, null, 2))}</div>
    `;
    el.olderOutputsPanel.appendChild(row);
  }

  function resetOlderOutputs() {
    el.olderLogsPre.innerHTML = "";
    el.olderOutputsPanel.innerHTML = `<div class="empty">No outputs yet.</div>`;
    el.olderFailureBanner.hidden = true;
  }

  function loadRunDetailsFromMarkdown(md) {
    const parsed = parseRunMarkdown(md);
    
    resetOlderOutputs();
    el.olderRunDetailCard.hidden = false;
    selectTab("older-logs", el.olderRunDetailCard);
    
    el.olderRunStatus.textContent = parsed.status === "success" ? "Succeeded" : (parsed.status === "failed" ? "Failed" : "Unknown");
    el.olderRunStatus.dataset.state = parsed.status;
    
    if (parsed.command) {
      appendOlderLog(`$ ${parsed.command}`);
    }
    if (parsed.logs) {
      parsed.logs.split("\n").forEach(line => {
        const isStderr = line.startsWith("[stderr] ");
        appendOlderLog(line, isStderr ? "stderr" : "stdout");
      });
    } else {
      if (parsed.stdout) {
        parsed.stdout.split("\n").forEach(line => appendOlderLog(line, "stdout"));
      }
      if (parsed.stderr) {
        parsed.stderr.split("\n").forEach(line => appendOlderLog(line, "stderr"));
      }
    }
    
    if (parsed.input) {
      appendOlderOutput("Input Parameters", parsed.input);
    }
    if (parsed.output) {
      appendOlderOutput("Output Data", parsed.output);
    }
    if (parsed.error) {
      appendOlderOutput("Error", parsed.error);
    }

    if (parsed.status === "failed") {
      el.olderFailureBanner.hidden = false;
    } else {
      el.olderFailureBanner.hidden = true;
    }
  }

  async function retryRun(runId) {
    try {
      const resp = await request(`/api/tasks/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}`);
      if (resp && resp.data_md) {
        const parsed = parseRunMarkdown(resp.data_md);
        if (parsed.input) {
          renderParamFields(parsed.input);
          validateForm();
          startScriptRun(parsed.input);
        } else {
          alert("No inputs found in this run to retry.");
        }
      }
    } catch (err) {
      alert("Failed to retry run: " + err.message);
    }
  }

  el.runsList.addEventListener("click", async (e) => {
    const retryBtn = e.target.closest(".retry-run-btn");
    if (retryBtn) {
      e.stopPropagation();
      e.preventDefault();
      const runId = retryBtn.dataset.runId;
      if (runId) {
        retryRun(runId);
      }
      return;
    }

    const item = e.target.closest(".run-item");
    if (!item) return;
    const runId = item.dataset.runId;
    if (!runId) return;

    try {
      const resp = await request(`/api/tasks/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}`);
      if (resp && resp.data_md) {
        loadRunDetailsFromMarkdown(resp.data_md);
      }
    } catch (err) {
      alert("Failed to load run details: " + err.message);
    }
  });

  el.messageInput.addEventListener("focus", () => {
    setMode("agent_typing");
  });

  if (el.toggleOlderRunsBtn) {
    el.toggleOlderRunsBtn.addEventListener("click", () => {
      const isHidden = el.olderRunsCard.hidden;
      el.olderRunsCard.hidden = !isHidden;
      el.toggleOlderRunsBtn.textContent = isHidden ? "Hide Older Runs" : "Show Older Runs";
    });
  }

  if (el.closeOlderDetailBtn) {
    el.closeOlderDetailBtn.addEventListener("click", () => {
      el.olderRunDetailCard.hidden = true;
    });
  }

  bootstrap();
})();
