(() => {
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
  };

  let sessions = [];
  let models = [];
  let currentSessionId = null;
  let messages = [];
  let sending = false;
  let streamController = null;

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

    function flushPara() {
      if (!para.length) return;
      blocks.push(`<p>${inlineMarkdown(para.join(" "))}</p>`);
      para = [];
    }

    function flushList() {
      if (!list.length) return;
      blocks.push(`<ul>${list.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ul>`);
      list = [];
    }

    function flushCode() {
      blocks.push(`<pre><code data-lang="${escapeAttr(codeLang)}">${escapeHtml(code.join("\n"))}</code></pre>`);
      code = [];
      codeLang = "";
    }

    for (const line of source.split("\n")) {
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
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        flushPara();
        flushList();
        const level = heading[1].length;
        blocks.push(`<h${level}>${inlineMarkdown(heading[2].trim())}</h${level}>`);
        continue;
      }
      const item = line.match(/^\s*[-*]\s+(.+)$/);
      if (item) {
        flushPara();
        list.push(item[1].trim());
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
    return session.summary || session.custom_title || session.first_prompt || session.session_id || "New chat";
  }

  function renderSessions() {
    if (!sessions.length) {
      el.sessionsList.innerHTML = `<div class="empty">No previous sessions.</div>`;
      return;
    }
    el.sessionsList.innerHTML = sessions.map((session) => {
      const sid = session.session_id || "";
      const active = sid && sid === currentSessionId;
      return `
        <button class="session-item ${active ? "active" : ""}" data-session-id="${escapeHtml(sid)}">
          <div class="session-title">${escapeHtml(sessionTitle(session))}</div>
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
      el.messages.innerHTML = `<div class="empty-state">Start a new workspace debugging chat.</div>`;
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

  function setSending(value) {
    sending = value;
    el.sendBtn.disabled = value;
    el.input.disabled = value;
    el.modelSelect.disabled = value;
    el.sendBtn.textContent = value ? "Sending" : "Send";
    if (el.stopBtn) el.stopBtn.hidden = !value;
  }

  async function loadSessions() {
    try {
      const data = await request("/api/agent/sessions");
      sessions = Array.isArray(data.sessions) ? data.sessions.filter((s) => s.session_id) : [];
      models = Array.isArray(data.models) ? data.models : models;
      if (models.length && !el.modelSelect.options.length) renderModels(data.default_model);
      if (!currentSessionId && data.active_session_id) currentSessionId = data.active_session_id;
      if (el.bashApprovalToggle && typeof data.bash_requires_approval === "boolean") {
        el.bashApprovalToggle.checked = data.bash_requires_approval;
      }
      renderSessions();
      renderHeader();
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
      const data = await request(`/api/agent/sessions/${encodeURIComponent(sessionId)}/messages`);
      messages = Array.isArray(data.messages) ? data.messages : [];
      renderMessages();
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
    el.input.focus();
  }

  function findToolMessage(toolId) {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === "tool" && messages[i].toolId === toolId) return messages[i];
    }
    return null;
  }

  function handleStreamEvent(event, ctx) {
    if (!event || typeof event !== "object") return;
    if (event.event === "text" && typeof event.text === "string") {
      ctx.assistant.loading = false;
      ctx.assistant.text = (ctx.assistant.text || "") + event.text;
    } else if (event.event === "tool_use") {
      const idx = messages.indexOf(ctx.assistant);
      const toolMsg = {
        role: "tool",
        toolId: event.id,
        toolName: event.name,
        toolInput: event.input,
      };
      if (idx >= 0) {
        messages.splice(idx, 0, toolMsg);
      } else {
        messages.push(toolMsg);
      }
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
      ctx.assistant.loading = false;
      ctx.assistant.text = (ctx.assistant.text || "") + "\n\n_(interrupted)_";
    } else if (event.event === "error") {
      setError(event.message || "Stream error");
      ctx.assistant.loading = false;
    } else if (event.event === "done") {
      ctx.assistant.loading = false;
      if (event.session_id) currentSessionId = event.session_id;
      if (event.status && event.status !== "success" && event.error) {
        setError(`${event.status}: ${event.error}`);
      }
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
    const assistantMsg = { role: "assistant", text: "", loading: true };
    messages.push({ role: "user", text });
    messages.push(assistantMsg);
    renderMessages();
    setSending(true);
    streamController = new AbortController();
    try {
      const response = await fetch("/api/agent/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
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
      await request("/api/agent/interrupt", { method: "POST" });
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
      await request("/api/agent/permission", {
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
        await request("/api/agent/settings/bash_requires_approval", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: el.bashApprovalToggle.checked }),
        });
      } catch (e) {
        setError(e.message || String(e));
        el.bashApprovalToggle.checked = !el.bashApprovalToggle.checked;
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
      const data = await request("/api/agent/models");
      models = Array.isArray(data.models) ? data.models : [];
      renderModels(data.default_model);
    } catch {
      models = [{ id: "default", label: "Default", description: "" }];
      renderModels("default");
    }
  }

  loadModels().then(loadSessions);
})();
