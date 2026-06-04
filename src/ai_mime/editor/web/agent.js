(() => {
  const shell = document.querySelector(".agent-shell");
  const taskId = shell && shell.dataset ? shell.dataset.taskId : "";
  const explicitPrefix = shell && shell.dataset ? shell.dataset.apiPrefix : "";
  const emptyMessage = shell && shell.dataset && shell.dataset.emptyMessage
    ? shell.dataset.emptyMessage
    : "Start a new workspace debugging chat.";
  const apiPrefix = explicitPrefix
    ? explicitPrefix
    : (taskId ? `/api/tasks/${encodeURIComponent(taskId)}/agent` : "/api/agent");
  const el = {
    newChatBtn: document.getElementById("newChatBtn"),
    sessionsList: document.getElementById("sessionsList"),
    chatTitle: document.getElementById("chatTitle"),
    messages: document.getElementById("messages"),
    errorBox: document.getElementById("errorBox"),
    form: document.getElementById("chatForm"),
    input: document.getElementById("messageInput"),
    modelSelect: document.getElementById("modelSelect"),
    sendBtn: document.getElementById("sendBtn"),
    stopBtn: document.getElementById("stopBtn"),
    bashApprovalToggle: document.getElementById("bashApprovalToggle"),
    toggleOverlayBtn: document.getElementById("toggleOverlayBtn"),
  };

  let sessions = [];
  let models = [];
  let currentSessionId = null;
  let messages = [];
  let sending = false;
  let streamController = null;
  let pendingReplayContext = "";
  let activeRuntime = null;

  if (taskId && explicitPrefix && explicitPrefix.includes("/replay-agent")) {
    try {
      pendingReplayContext = sessionStorage.getItem(`replay:agent-context:${taskId}`) || "";
    } catch {
      pendingReplayContext = "";
    }
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function escapeAttr(s) {
    return escapeHtml(s).replaceAll('"', "&quot;");
  }

  function inlineMarkdown(s) {
    let out = escapeHtml(s);
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    return out;
  }

  function renderMarkdown(text) {
    const source = String(text == null ? "" : text).replace(/\r\n/g, "\n");
    const blocks = [];
    let inCode = false;
    let code = [];
    let codeLang = "";
    let para = [];
    let list = [];
    let listType = "ul";
    let listStart = 1;

    function flushPara() {
      if (!para.length) return;
      blocks.push(`<p>${inlineMarkdown(para.join(" "))}</p>`);
      para = [];
    }

    function flushList() {
      if (!list.length) return;
      const items = list.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("");
      if (listType === "ol") {
        const startAttr = listStart && listStart !== 1 ? ` start="${listStart}"` : "";
        blocks.push(`<ol${startAttr}>${items}</ol>`);
      } else {
        blocks.push(`<ul>${items}</ul>`);
      }
      list = [];
      listType = "ul";
      listStart = 1;
    }

    function flushCode() {
      blocks.push(`<pre><code data-lang="${escapeAttr(codeLang)}">${escapeHtml(code.join("\n"))}</code></pre>`);
      code = [];
      codeLang = "";
    }

    function splitRow(row) {
      let s = row.trim();
      if (s.startsWith("|")) s = s.slice(1);
      if (s.endsWith("|")) s = s.slice(0, -1);
      return s.split("|").map((c) => c.trim());
    }

    function isTableSeparator(line) {
      const trimmed = line.trim();
      if (!trimmed.includes("|")) return false;
      return /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(trimmed);
    }

    const lines = source.split("\n");
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      const fence = line.match(/^```(.*)$/);
      if (fence) {
        if (inCode) {
          flushCode();
          inCode = false;
        } else {
          flushPara();
          flushList();
          inCode = true;
          codeLang = fence[1].trim();
        }
        continue;
      }
      if (inCode) {
        code.push(line);
        continue;
      }
      if (!line.trim()) {
        flushPara();
        flushList();
        continue;
      }
      if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
        flushPara();
        flushList();
        blocks.push("<hr>");
        continue;
      }
      if (line.includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        flushPara();
        flushList();
        const header = splitRow(line);
        const aligns = splitRow(lines[i + 1]).map((c) => {
          const left = c.startsWith(":");
          const right = c.endsWith(":");
          if (left && right) return "center";
          if (right) return "right";
          if (left) return "left";
          return "";
        });
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
          rows.push(splitRow(lines[i]));
          i += 1;
        }
        i -= 1;
        const thead = `<thead><tr>${header.map((c, idx) => {
          const a = aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
          return `<th${a}>${inlineMarkdown(c)}</th>`;
        }).join("")}</tr></thead>`;
        const tbody = `<tbody>${rows.map((r) => `<tr>${r.map((c, idx) => {
          const a = aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
          return `<td${a}>${inlineMarkdown(c)}</td>`;
        }).join("")}</tr>`).join("")}</tbody>`;
        blocks.push(`<table>${thead}${tbody}</table>`);
        continue;
      }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        flushPara();
        flushList();
        const level = heading[1].length;
        blocks.push(`<h${level}>${inlineMarkdown(heading[2].trim())}</h${level}>`);
        continue;
      }
      const ulItem = line.match(/^\s*[-*]\s+(.+)$/);
      if (ulItem) {
        flushPara();
        if (list.length && listType !== "ul") flushList();
        listType = "ul";
        list.push(ulItem[1].trim());
        continue;
      }
      const olItem = line.match(/^\s*(\d+)[.)]\s+(.+)$/);
      if (olItem) {
        flushPara();
        if (list.length && listType !== "ol") flushList();
        if (!list.length) {
          listType = "ol";
          listStart = parseInt(olItem[1], 10) || 1;
        }
        list.push(olItem[2].trim());
        continue;
      }
      para.push(line.trim());
    }
    if (inCode) flushCode();
    flushPara();
    flushList();
    return blocks.join("");
  }

  async function request(path, options = {}) {
    const res = await fetch(path, options);
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      // Keep raw text for errors.
    }
    if (!res.ok) {
      const detail = data && data.detail ? data.detail : text || `HTTP ${res.status}`;
      throw new Error(detail);
    }
    return data;
  }

  function setError(text) {
    if (!text) {
      el.errorBox.hidden = true;
      el.errorBox.textContent = "";
      return;
    }
    el.errorBox.hidden = false;
    el.errorBox.textContent = text;
  }

  function sessionTitle(session) {
    const rawTitle = session.summary || session.custom_title || session.first_prompt || session.session_id || "New chat";
    const mode = session.mode;
    if (mode === "Build" || mode === "Improve" || mode === "Run") {
      return `${mode}: ${rawTitle}`;
    }
    return rawTitle;
  }

  function runtimeLabel(runtimeId) {
    if (runtimeId === "claude_code" || runtimeId === "claude") return "Claude Code";
    if (runtimeId === "codex_cli") return "Codex CLI";
    return runtimeId || "";
  }

  function sessionMeta(session) {
    const parts = [];
    const runtime = runtimeLabel(session.runtime_id);
    if (runtime) parts.push(runtime);
    if (session.model) parts.push(session.model);
    return parts.join(" · ");
  }

  function renderSessions() {
    if (!sessions.length) {
      el.sessionsList.innerHTML = `<div class="empty">No previous sessions.</div>`;
      return;
    }
    el.sessionsList.innerHTML = sessions.map((session) => {
      const sid = session.session_id || "";
      const active = sid && sid === currentSessionId;
      const meta = sessionMeta(session);
      return `
        <button class="session-item ${active ? "active" : ""}" data-session-id="${escapeHtml(sid)}">
          <div class="session-title">${escapeHtml(sessionTitle(session))}</div>
          ${meta ? `<div class="session-meta">${escapeHtml(meta)}</div>` : ""}
        </button>
      `;
    }).join("");
  }

  function renderModels(defaultModel) {
    el.modelSelect.innerHTML = models.map((model) => {
      const id = model.id || "";
      const label = model.label || id;
      const description = model.description || "";
      const selected = id === defaultModel ? " selected" : "";
      return `<option value="${escapeAttr(id)}" title="${escapeAttr(description)}"${selected}>${escapeHtml(label)}</option>`;
    }).join("");
    updateModelSelectWidth();
  }

  function updateModelSelectWidth() {
    const selected = el.modelSelect.options[el.modelSelect.selectedIndex];
    const text = selected ? selected.textContent || selected.value || "" : "";
    const width = Math.ceil(Math.max(112, Math.min(320, text.length * 7.2 + 42)));
    el.modelSelect.style.setProperty("--model-select-width", `${width}px`);
  }

  function getStoredMessageText(message) {
    const raw = message && message.message;
    if (typeof raw === "string") return raw;
    if (raw && typeof raw === "object") {
      if (typeof raw.content === "string") return raw.content;
      if (Array.isArray(raw.content)) {
        return raw.content.map((part) => {
          if (typeof part === "string") return part;
          if (part && typeof part.text === "string") return part.text;
          return "";
        }).filter(Boolean).join("\n");
      }
    }
    return raw == null ? "" : JSON.stringify(raw, null, 2);
  }

  function isVisibleMessage(message) {
    const role = message.role || message.type || "assistant";
    if (role !== "user" && role !== "assistant") return false;
    const text = message.text != null ? message.text : getStoredMessageText(message);
    if (typeof text !== "string") return true;
    const trimmed = text.trim();
    if (!trimmed) return false;
    if (role === "user" && /^<command-(name|message|args)>/.test(trimmed)) return false;
    return true;
  }

  function renderToolLog(message) {
    const name = message.toolName || "tool";
    const statusLabel = message.toolError
      ? '<span class="tool-status error">error</span>'
      : message.toolDenied
        ? '<span class="tool-status denied">denied</span>'
        : message.toolComplete
          ? '<span class="tool-status ok">done</span>'
          : '<span class="tool-status">running…</span>';
    const inputJson = message.toolInput != null ? JSON.stringify(message.toolInput, null, 2) : "";
    const output = message.toolOutput != null ? String(message.toolOutput) : "";
    const body = `${inputJson ? `<pre>$ ${escapeHtml(name)} ${escapeHtml(inputJson)}</pre>` : ""}${output ? `<pre>${escapeHtml(output)}</pre>` : ""}`;
    return `
      <details class="tool-log"${message.toolError || message.toolDenied ? " open" : ""}>
        <summary><span class="tool-name">${escapeHtml(name)}</span>${statusLabel}</summary>
        ${body}
      </details>
    `;
  }

  function renderPermissionPrompt(message) {
    const cmd = message.permInput && (message.permInput.command || message.permInput.cmd);
    const inputJson = message.permInput != null ? JSON.stringify(message.permInput, null, 2) : "";
    if (message.permResolved) {
      return `
        <div class="permission-prompt" style="opacity:0.6">
          <div class="perm-title">${escapeHtml(message.permToolName || "Tool")} — ${escapeHtml(message.permResolved)}</div>
          ${inputJson ? `<pre>${escapeHtml(inputJson)}</pre>` : ""}
        </div>
      `;
    }
    return `
      <div class="permission-prompt" data-request-id="${escapeAttr(message.permRequestId)}">
        <div class="perm-title">${escapeHtml(message.permToolName || "Tool")} wants to run</div>
        ${cmd ? `<pre>${escapeHtml(String(cmd))}</pre>` : (inputJson ? `<pre>${escapeHtml(inputJson)}</pre>` : "")}
        <div class="perm-actions">
          <button class="allow" data-decision="allow">Allow once</button>
          ${message.permToolName === "Bash" ? `<button data-decision="allow_always">Allow for this session</button>` : ""}
          <button class="deny" data-decision="deny">Deny</button>
        </div>
      </div>
    `;
  }

  function renderMessages() {
    const visible = messages.filter((message) =>
      message.loading || message.role === "tool" || message.role === "permission" || isVisibleMessage(message)
    );
    if (!visible.length) {
      el.messages.innerHTML = `<div class="empty-state">${escapeHtml(emptyMessage)}</div>`;
      return;
    }
    el.messages.innerHTML = visible.map((message) => {
      if (message.role === "permission") return renderPermissionPrompt(message);
      if (message.role === "tool") return renderToolLog(message);
      const role = message.role || message.type || "assistant";
      const text = message.text != null ? message.text : getStoredMessageText(message);
      const safeRole = role === "user" ? "user" : "assistant";
      const loading = message.loading ? " loading" : "";
      const bubble = message.loading && !text
        ? `<div class="typing"><span></span><span></span><span></span></div>`
        : (safeRole === "assistant" ? renderMarkdown(text) : escapeHtml(text));
      return `
        <div class="message ${safeRole}${loading}">
          <div class="bubble">${bubble}</div>
        </div>
      `;
    }).join("");
    el.messages.scrollTop = el.messages.scrollHeight;
  }

  function renderHeader() {
    const active = sessions.find((session) => session.session_id === currentSessionId);
    el.chatTitle.textContent = active ? sessionTitle(active) : "New Chat";
    if (active && active.model && Array.from(el.modelSelect.options).some((option) => option.value === active.model)) {
      el.modelSelect.value = active.model;
    }
  }

  function checkComposerState() {
    if (sending) {
      el.sendBtn.disabled = true;
      el.input.disabled = true;
      el.modelSelect.disabled = true;
      return;
    }
    if (!currentSessionId) {
      el.sendBtn.disabled = false;
      el.input.disabled = false;
      el.modelSelect.disabled = false;
      return;
    }
    const active = sessions.find((s) => s.session_id === currentSessionId);
    const sessionRuntime = active && active.runtime_id;
    if (sessionRuntime && activeRuntime && sessionRuntime !== activeRuntime) {
      el.sendBtn.disabled = true;
      el.input.disabled = true;
      el.modelSelect.disabled = true;
      setError(`This conversation uses ${runtimeLabel(sessionRuntime)} (${sessionRuntime}). Switch your agent runtime to ${sessionRuntime} to continue here. Active runtime: ${runtimeLabel(activeRuntime)} (${activeRuntime}).`);
    } else {
      el.sendBtn.disabled = false;
      el.input.disabled = false;
      el.modelSelect.disabled = false;
      if (el.errorBox && el.errorBox.textContent.includes("Switch your agent runtime to continue it")) {
        setError("");
      }
    }
  }

  function setSending(value) {
    sending = value;
    el.sendBtn.textContent = value ? "Sending" : "Send";
    if (el.stopBtn) el.stopBtn.hidden = !value;
    checkComposerState();
  }

  function applyBashToggleSupport(supported) {
    if (!el.bashApprovalToggle) return;
    // `supported` is false for runtimes that ignore the gate (e.g. Codex, which
    // runs with full access and no per-command approval). Default to supported
    // when the field is absent. For unsupported runtimes we replace the toggle
    // with a "Full Access!" badge instead of a dimmed, inert checkbox.
    const isSupported = supported !== false;
    const label = el.bashApprovalToggle.closest(".bash-toggle");
    const span = label ? label.querySelector("span") : null;
    el.bashApprovalToggle.disabled = !isSupported;
    el.bashApprovalToggle.style.display = isSupported ? "" : "none";
    if (label) {
      label.classList.toggle("bash-toggle--full-access", !isSupported);
      label.title = isSupported
        ? ""
        : "Codex runs with full access — commands are not gated. For per-command approval, switch the agent to Claude.";
    }
    if (span) {
      span.textContent = isSupported ? "Require approval for Bash" : "Full Access!";
    }
  }

  async function loadSessions() {
    try {
      const data = await request(`${apiPrefix}/sessions`);
      sessions = Array.isArray(data.sessions) ? data.sessions.filter((s) => s.session_id) : [];
      models = Array.isArray(data.models) ? data.models : models;
      if (models.length && !el.modelSelect.options.length) renderModels(data.default_model);
      if (!currentSessionId && data.active_session_id) currentSessionId = data.active_session_id;
      if (el.bashApprovalToggle && typeof data.bash_requires_approval === "boolean") {
        el.bashApprovalToggle.checked = data.bash_requires_approval;
      }
      applyBashToggleSupport(data.bash_requires_approval_supported);
      if (data.active_runtime) activeRuntime = data.active_runtime;
      renderSessions();
      renderHeader();
      checkComposerState();
      try {
        window.dispatchEvent(new CustomEvent("agent-sessions-loaded", {
          detail: { active_session_id: currentSessionId, sessions },
        }));
      } catch {
        // ignore
      }
    } catch (e) {
      setError(e.message || String(e));
    }
  }

  async function loadMessages(sessionId) {
    setError("");
    currentSessionId = sessionId;
    messages = [];
    renderMessages();
    renderSessions();
    renderHeader();
    try {
      const data = await request(`${apiPrefix}/sessions/${encodeURIComponent(sessionId)}/messages`);
      messages = Array.isArray(data.messages) ? data.messages : [];
      renderMessages();
      checkComposerState();
      try {
        window.dispatchEvent(new CustomEvent("agent-session-loaded", {
          detail: { session_id: sessionId, messages },
        }));
      } catch {
        // ignore
      }
    } catch (e) {
      setError(e.message || String(e));
    }
  }

  async function newChat() {
    setError("");
    currentSessionId = null;
    messages = [];
    renderMessages();
    renderHeader();
    renderSessions();
    checkComposerState();
    el.input.focus();
    try {
      window.dispatchEvent(new CustomEvent("agent-session-loaded", {
        detail: { session_id: null, messages: [] },
      }));
    } catch {
      // ignore
    }
  }

  function findToolMessage(toolId) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === "tool" && messages[i].toolId === toolId) return messages[i];
    }
    return null;
  }

  function ensureAssistant(ctx) {
    if (ctx.assistant && messages.indexOf(ctx.assistant) >= 0) return ctx.assistant;
    const fresh = { role: "assistant", text: "", loading: true };
    messages.push(fresh);
    ctx.assistant = fresh;
    return fresh;
  }

  function handleStreamEvent(event, ctx) {
    if (!event || typeof event !== "object") return;
    try {
      window.dispatchEvent(new CustomEvent("agent-stream-event", { detail: event }));
    } catch {
      // ignore
    }
    if (event.event === "text" && typeof event.text === "string") {
      const target = ensureAssistant(ctx);
      target.loading = false;
      target.text = (target.text || "") + event.text;
    } else if (event.event === "tool_use") {
      if (ctx.assistant) {
        ctx.assistant.loading = false;
        if (!ctx.assistant.text) {
          const idx = messages.indexOf(ctx.assistant);
          if (idx >= 0) messages.splice(idx, 1);
        }
      }
      ctx.assistant = null;
      messages.push({
        role: "tool",
        toolId: event.id,
        toolName: event.name,
        toolInput: event.input,
      });
    } else if (event.event === "tool_result") {
      const existing = findToolMessage(event.tool_use_id);
      const content = Array.isArray(event.content)
        ? event.content.map((c) => (typeof c === "string" ? c : (c && c.text) || JSON.stringify(c))).join("\n")
        : (event.content == null ? "" : String(event.content));
      const isDenied = !!event.is_error && /blocked|not enabled|outside/i.test(content);
      if (existing) {
        existing.toolOutput = content;
        existing.toolError = !!event.is_error && !isDenied;
        existing.toolDenied = isDenied;
        existing.toolComplete = true;
      } else {
        messages.push({
          role: "tool",
          toolId: event.tool_use_id,
          toolName: "result",
          toolOutput: content,
          toolError: !!event.is_error && !isDenied,
          toolDenied: isDenied,
          toolComplete: true,
        });
      }
    } else if (event.event === "permission_request") {
      messages.push({
        role: "permission",
        permRequestId: event.request_id,
        permToolName: event.tool_name,
        permInput: event.input,
        permReason: event.reason,
      });
    } else if (event.event === "interrupted") {
      const target = ensureAssistant(ctx);
      target.loading = false;
      target.text = (target.text || "") + "\n\n_(interrupted)_";
    } else if (event.event === "error") {
      setError(event.message || "Stream error");
      if (ctx.assistant) ctx.assistant.loading = false;
    } else if (event.event === "done") {
      if (ctx.assistant) ctx.assistant.loading = false;
      if (event.session_id) currentSessionId = event.session_id;
      if (event.status && event.status !== "success" && event.error) {
        setError(`${event.status}: ${event.error}`);
      }
    } else {
      // Unknown event types are already forwarded above for panel-specific scripts.
    }
  }

  async function consumeSseStream(response, ctx) {
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
            handleStreamEvent(JSON.parse(payload), ctx);
          } catch {
            // ignore malformed event
          }
        }
        renderMessages();
      }
    }
  }

  async function sendMessage(text) {
    setError("");
    const outboundText = pendingReplayContext
      ? `${pendingReplayContext}\n\nUser request:\n${text}`
      : text;
    if (pendingReplayContext) {
      pendingReplayContext = "";
      try { sessionStorage.removeItem(`replay:agent-context:${taskId}`); } catch { /* ignore */ }
    }
    const assistantMsg = { role: "assistant", text: "", loading: true };
    messages.push({ role: "user", text });
    messages.push(assistantMsg);
    renderMessages();
    setSending(true);
    streamController = new AbortController();
    try {
      const response = await fetch(`${apiPrefix}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: outboundText,
          session_id: currentSessionId || null,
          model: el.modelSelect.value || null,
        }),
        signal: streamController.signal,
      });
      if (!response.ok) {
        const errText = await response.text();
        throw new Error(errText || `HTTP ${response.status}`);
      }
      await consumeSseStream(response, { assistant: assistantMsg });
      assistantMsg.loading = false;
      renderMessages();
      await loadSessions();
    } catch (e) {
      assistantMsg.loading = false;
      if (e.name !== "AbortError") setError(e.message || String(e));
      renderMessages();
    } finally {
      streamController = null;
      setSending(false);
      el.input.focus();
    }
  }

  async function interruptCurrent() {
    try {
      await request(`${apiPrefix}/interrupt`, { method: "POST" });
    } catch (e) {
      setError(e.message || String(e));
    }
  }

  el.newChatBtn.addEventListener("click", newChat);

  if (el.stopBtn) {
    el.stopBtn.addEventListener("click", () => {
      if (!sending) return;
      interruptCurrent();
    });
  }

  el.messages.addEventListener("click", async (event) => {
    const button = event.target.closest(".permission-prompt button[data-decision]");
    if (!button) return;
    const panel = button.closest(".permission-prompt");
    const requestId = panel ? panel.dataset.requestId : "";
    const decision = button.dataset.decision;
    if (!requestId || !decision) return;
    const msg = messages.find((m) => m.role === "permission" && m.permRequestId === requestId);
    if (msg) {
      msg.permResolved = decision === "deny" ? "denied" : (decision === "allow_always" ? "allowed (session)" : "allowed");
      renderMessages();
    }
    try {
      await request(`${apiPrefix}/permission`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, decision }),
      });
    } catch (e) {
      setError(e.message || String(e));
    }
  });

  if (el.bashApprovalToggle) {
    el.bashApprovalToggle.addEventListener("change", async () => {
      try {
        const data = await request(`${apiPrefix}/settings/bash_requires_approval`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: el.bashApprovalToggle.checked }),
        });
        if (data && typeof data.bash_requires_approval === "boolean") {
          el.bashApprovalToggle.checked = data.bash_requires_approval;
        }
        if (data) applyBashToggleSupport(data.bash_requires_approval_supported);
      } catch (e) {
        setError(e.message || String(e));
        el.bashApprovalToggle.checked = !el.bashApprovalToggle.checked;
      }
    });
  }

  el.modelSelect.addEventListener("change", updateModelSelectWidth);

  if (el.toggleOverlayBtn) {
    el.toggleOverlayBtn.addEventListener("click", async () => {
      try {
        await request("/api/overlay/toggle", { method: "POST" });
      } catch (e) {
        setError(e.message || String(e));
      }
    });
  }

  el.sessionsList.addEventListener("click", (event) => {
    const button = event.target.closest(".session-item[data-session-id]");
    if (!button || sending) return;
    const sid = button.dataset.sessionId;
    if (sid) loadMessages(sid);
  });

  el.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (sending) return;
    const text = el.input.value.trim();
    if (!text) return;
    el.input.value = "";
    await sendMessage(text);
  });

  el.input.addEventListener("input", () => {
    el.input.style.height = "auto";
    el.input.style.height = `${Math.min(el.input.scrollHeight, 170)}px`;
  });

  el.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      el.form.requestSubmit();
    }
  });

  async function loadModels() {
    try {
      const data = await request(`${apiPrefix}/models`);
      models = Array.isArray(data.models) ? data.models : [];
      renderModels(data.default_model);
    } catch {
      models = [{ id: "default", label: "Default", description: "" }];
      renderModels("default");
    }
  }

  window.AgentChat = {
    newChat,
    loadMessages,
    sendMessage,
    currentSessionId: () => currentSessionId,
  };

  loadModels().then(loadSessions);
})();
