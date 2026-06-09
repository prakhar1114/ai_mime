(() => {
  const el = {
    taskList: document.getElementById("taskList"),
    refreshBtn: document.getElementById("refreshBtn"),
    providerBtn: document.getElementById("providerBtn"),
    startRecordingBtn: document.getElementById("startRecordingBtn"),
    directBuildBtn: document.getElementById("directBuildBtn"),
    uploadSkillBtn: document.getElementById("uploadSkillBtn"),
    exploreMarketplaceBtn: document.getElementById("exploreMarketplaceBtn"),
    agentModeBtn: document.getElementById("agentModeBtn"),
    openWorkflowsBtn: document.getElementById("openWorkflowsBtn"),
    quitAppBtn: document.getElementById("quitAppBtn"),
    syncState: document.getElementById("syncState"),
    totalCount: document.getElementById("totalCount"),
    readyCount: document.getElementById("readyCount"),
    attentionCount: document.getElementById("attentionCount"),
    activeCount: document.getElementById("activeCount"),
  };

  const ACTIVE = new Set(["reflecting", "compiling", "replaying", "deleting"]);
  const ATTENTION = new Set(["pending_reflection", "failed_reflection", "replay_failed"]);
  let tasks = [];
  let appStatus = {};
  let providerSettings = null;
  let busy = false;
  let openMenuTaskId = null;

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function statusLabel(status) {
    return {
      ready: "Ready",
      pending_reflection: "Reflection needed",
      reflecting: "Reflecting",
      compiling: "Compiling",
      failed_reflection: "Reflection failed",
      replaying: "Replaying",
      replay_failed: "Replay failed",
      deleting: "Deleting",
    }[status] || status || "Unknown";
  }

  async function request(path, options = {}) {
    const res = await fetch(path, options);
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      // Keep raw text for error reporting.
    }
    if (!res.ok) {
      const detail = data && data.detail ? data.detail : text || `HTTP ${res.status}`;
      throw new Error(detail);
    }
    return data;
  }

  function setSync(text) {
    el.syncState.textContent = text;
  }

  function renderSummary() {
    const ready = tasks.filter((t) => t.status === "ready").length;
    const attention = tasks.filter((t) => ATTENTION.has(t.status)).length;
    const active = tasks.filter((t) => ACTIVE.has(t.status)).length;
    el.totalCount.textContent = String(tasks.length);
    el.readyCount.textContent = String(ready);
    el.attentionCount.textContent = String(attention);
    el.activeCount.textContent = String(active);
  }

  function render() {
    renderSummary();
    renderAppStatus();
    if (!tasks.length) {
      el.taskList.innerHTML = `<div class="empty">No tasks found.</div>`;
      return;
    }
    el.taskList.innerHTML = tasks.map((task) => {
      const status = escapeHtml(task.status);
      const error = task.error ? `<div class="error">${escapeHtml(task.error)}</div>` : "";
      let reflectItems = [];
      const isReflectingOrCompiling = task.status === "reflecting" || task.status === "compiling";
      const canShowReflect = task.can_reflect || isReflectingOrCompiling;

      if (canShowReflect) {
        if (isReflectingOrCompiling) {
          const reflectText = task.status === "reflecting" ? "Reflecting..." : "Compiling...";
          reflectItems.push(`<button class="menu-item" data-action="reflect">${escapeHtml(reflectText)}</button>`);
        } else if (task.has_optimized_plan) {
          if (task.has_skill) {
            reflectItems.push(`<button class="menu-item" data-action="continue-improve">Continue Improving Skill</button>`);
            reflectItems.push(`<button class="menu-item" data-action="edit-skill">Edit Skill (New Session)</button>`);
            reflectItems.push(`<button class="menu-item" data-action="run-skill">Run</button>`);
          } else {
            reflectItems.push(`<button class="menu-item" data-action="continue-improve">Build Skill</button>`);
          }
        } else {
          const reflectText = task.status === "failed_reflection" ? "Retry reflection" : "Reflect";
          reflectItems.push(`<button class="menu-item" data-action="reflect">${escapeHtml(reflectText)}</button>`);
        }
      }

      const menuItems = [
        ...reflectItems,
        task.can_delete ? `<button class="menu-item danger" data-action="delete">Delete</button>` : "",
      ].filter(Boolean).join("");
      const menuOpen = openMenuTaskId === task.id;
      return `
        <div class="task-row" data-id="${escapeHtml(task.id)}">
          <div class="task-title">
            <div class="task-name">${escapeHtml(task.display_name || task.id)}</div>
            <div class="task-id">${escapeHtml(task.id)}</div>
          </div>
          <div>
            <span class="status ${status}">
              <span class="dot"></span>
              <span>${escapeHtml(statusLabel(task.status))}</span>
            </span>
          </div>
          <div class="actions">
            <button class="icon-btn play-btn" data-action="replay" ${task.can_replay ? "" : "disabled"} title="Replay" aria-label="Replay">
              <span class="play-icon"></span>
            </button>
            <div class="overflow">
              <button class="icon-btn overflow-btn" data-action="menu" ${menuItems ? "" : "disabled"} title="More actions" aria-label="More actions">...</button>
              <div class="menu ${menuOpen ? "open" : ""}">
                ${menuItems || `<div class="menu-empty">No actions</div>`}
              </div>
            </div>
          </div>
          ${error}
        </div>
      `;
    }).join("");
  }

  function renderAppStatus() {
    const isRecording = !!appStatus.is_recording;
    const requested = !!appStatus.recording_requested;
    el.startRecordingBtn.disabled = isRecording || requested;
    if (isRecording) {
      el.startRecordingBtn.textContent = "Recording...";
    } else if (requested) {
      el.startRecordingBtn.textContent = "Opening recorder...";
    } else {
      el.startRecordingBtn.textContent = "Start recording";
    }
  }

  function providerLabel(provider) {
    const settings = providerSettings && providerSettings.providers && providerSettings.providers[provider];
    return settings && settings.label ? settings.label : (provider || "Provider");
  }

  function renderProviderButton() {
    if (!el.providerBtn) return;
    const provider = providerSettings && providerSettings.provider;
    el.providerBtn.textContent = provider ? providerLabel(provider) : "Provider";
  }

  async function loadProviderSettings() {
    try {
      providerSettings = await request("/api/settings/provider");
    } catch {
      providerSettings = null;
    }
    renderProviderButton();
  }

  async function loadTasks() {
    if (busy) return;
    busy = true;
    setSync("Refreshing");
    try {
      const data = await request("/api/tasks");
      tasks = Array.isArray(data.tasks) ? data.tasks : [];
      appStatus = data.app && typeof data.app === "object" ? data.app : {};
      if (openMenuTaskId && !tasks.some((task) => task.id === openMenuTaskId)) openMenuTaskId = null;
      render();
      setSync(new Date().toLocaleTimeString());
    } catch (e) {
      el.taskList.innerHTML = `<div class="empty">Failed to load tasks: ${escapeHtml(e.message || String(e))}</div>`;
      setSync("Error");
    } finally {
      busy = false;
    }
  }

  async function runAction(taskId, action) {
    const task = tasks.find((t) => t.id === taskId);
    if (!task) return;
    const encoded = encodeURIComponent(taskId);
    try {
      if (action === "delete") {
        if (!confirm(`Delete ${taskId}? This removes the workflow and recording folders when present.`)) return;
        await request(`/api/tasks/${encoded}`, { method: "DELETE" });
      } else if (action === "reflect") {
        if (task.status === "reflecting" || task.status === "compiling") {
          window.location.href = `/reflect/${encoded}`;
          return;
        }
        await request(`/api/tasks/${encoded}/reflect`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ force: false }),
        });
        window.location.href = `/reflect/${encoded}`;
        return;
      } else if (action === "continue-improve") {
        window.location.href = `/skill-build/${encoded}?action=continue`;
        return;
      } else if (action === "edit-skill") {
        window.location.href = `/skill-build/${encoded}?action=new`;
        return;
      } else if (action === "run-skill") {
        window.location.href = `/replay/${encoded}`;
        return;
      } else if (action === "replay") {
        window.location.href = `/replay/${encoded}`;
        return;
      }
      await loadTasks();
    } catch (e) {
      alert(e.message || String(e));
      await loadTasks();
    }
  }

  function closeProviderModal() {
    const existing = document.querySelector(".modal-overlay.provider-modal");
    if (existing) existing.remove();
  }

  function closeDirectBuildModal() {
    const existing = document.querySelector(".modal-overlay.direct-build-modal");
    if (existing) existing.remove();
  }

  function closeImportModal() {
    const existing = document.querySelector(".modal-overlay.import-modal");
    if (existing) existing.remove();
  }

  function renderImportPreviewHtml(data) {
    const warnings = Array.isArray(data.warnings) && data.warnings.length
      ? `<ul>${data.warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
      : `<div class="modal-desc">No warnings.</div>`;
    const removed = Array.isArray(data.removed_preview) && data.removed_preview.length
      ? `<details class="import-details"><summary>${data.removed_preview.length} generated files will be removed</summary><ul>${data.removed_preview.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></details>`
      : `<div class="modal-desc">No generated files need to be removed.</div>`;
    return `
      <div class="import-summary">
        <div><strong>Type</strong><span>${escapeHtml(data.detected_type || "Unknown")}</span></div>
        <div><strong>Name</strong><span>${escapeHtml(data.display_name || "Imported Skill")}</span></div>
        <div><strong>Skill</strong><span>${escapeHtml(data.skill_name || "")}</span></div>
        <div><strong>Status</strong><span>${data.valid ? "Valid" : "Invalid"}</span></div>
      </div>
      <div class="modal-section-title">Warnings</div>
      ${warnings}
      <div class="modal-section-title">Cleanup</div>
      ${removed}
    `;
  }

  async function installImportedSkill(stagingId, button, message) {
    button.disabled = true;
    message.textContent = "Installing...";
    try {
      const data = await request("/api/import/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ staging_id: stagingId }),
      });
      const taskId = data && data.task_id;
      if (!taskId) throw new Error("Import installed without a task id.");
      const status = await request(`/api/tasks/${encodeURIComponent(taskId)}/status`);
      if (status && status.can_replay) {
        window.location.href = `/replay/${encodeURIComponent(taskId)}`;
      } else {
        window.location.href = `/skill-build/${encodeURIComponent(taskId)}?action=continue`;
      }
    } catch (e) {
      message.textContent = e.message || String(e);
      button.disabled = false;
    }
  }

  function showImportModal(files) {
    closeImportModal();
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay import-modal";
    overlay.innerHTML = `
      <div class="modal-card import-card" role="dialog" aria-modal="true" aria-label="Upload skill">
        <div class="modal-header">
          <div class="modal-title">Upload skill</div>
          <div class="modal-desc">Verifying the selected folder before installing it into Workflows.</div>
        </div>
        <div id="importPreviewBody" class="import-preview-body">
          <div class="modal-desc">Uploading and checking structure...</div>
        </div>
        <div class="provider-message" id="importMessage"></div>
        <div class="modal-actions row">
          <button class="modal-btn secondary" id="cancelImportBtn">Cancel</button>
          <button class="modal-btn primary" id="installImportBtn" disabled>Install</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const body = overlay.querySelector("#importPreviewBody");
    const message = overlay.querySelector("#importMessage");
    const installBtn = overlay.querySelector("#installImportBtn");
    overlay.querySelector("#cancelImportBtn").addEventListener("click", closeImportModal);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) closeImportModal();
    });

    const form = new FormData();
    Array.from(files || []).forEach((file) => {
      form.append("files", file, file.webkitRelativePath || file.name);
    });
    fetch("/api/import/preview", { method: "POST", body: form })
      .then(async (res) => {
        const text = await res.text();
        let data = null;
        try { data = text ? JSON.parse(text) : null; } catch { /* keep raw */ }
        if (!res.ok) {
          const detail = data && data.detail ? data.detail : text || `HTTP ${res.status}`;
          throw new Error(detail);
        }
        return data;
      })
      .then((data) => {
        body.innerHTML = renderImportPreviewHtml(data || {});
        installBtn.disabled = !(data && data.valid && data.staging_id);
        installBtn.addEventListener("click", () => installImportedSkill(data.staging_id, installBtn, message));
      })
      .catch((e) => {
        body.innerHTML = `<div class="modal-desc">The selected folder could not be imported.</div>`;
        message.textContent = e.message || String(e);
      });
  }

  function openUploadSkillPicker() {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.webkitdirectory = true;
    input.addEventListener("change", () => {
      if (input.files && input.files.length) showImportModal(input.files);
    });
    input.click();
  }

  function openDirectBuildModal() {
    closeDirectBuildModal();
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay direct-build-modal";
    overlay.innerHTML = `
      <div class="modal-card direct-build-card" role="dialog" aria-modal="true" aria-label="Direct build workflow">
        <div class="modal-header">
          <div class="modal-title">Direct build</div>
          <div class="modal-desc">Create a workflow and build a reusable skill directly from a task description.</div>
        </div>
        <label class="provider-key">
          <span>Workflow name</span>
          <input type="text" id="directBuildName" placeholder="e.g. Summarize invoices">
        </label>
        <div class="provider-message" id="directBuildMessage"></div>
        <div class="modal-actions row">
          <button class="modal-btn secondary" id="cancelDirectBuildBtn">Cancel</button>
          <button class="modal-btn primary" id="createDirectBuildBtn">Create Workflow</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const nameInput = overlay.querySelector("#directBuildName");
    const message = overlay.querySelector("#directBuildMessage");
    const createBtn = overlay.querySelector("#createDirectBuildBtn");
    const submit = async () => {
      const name = nameInput.value.trim();
      if (!name) {
        message.textContent = "Enter a workflow name.";
        nameInput.focus();
        return;
      }
      createBtn.disabled = true;
      message.textContent = "Creating workflow...";
      try {
        const data = await request("/api/direct-build/workflows", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        const taskId = data && data.task_id;
        if (!taskId) throw new Error("Direct build workflow was created without a task id.");
        window.location.href = `/skill-build/${encodeURIComponent(taskId)}?action=direct-start`;
      } catch (e) {
        message.textContent = e.message || String(e);
        createBtn.disabled = false;
      }
    };
    overlay.querySelector("#cancelDirectBuildBtn").addEventListener("click", closeDirectBuildModal);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) closeDirectBuildModal();
    });
    createBtn.addEventListener("click", submit);
    nameInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });
    nameInput.focus();
  }

  async function openProviderModal() {
    if (!providerSettings) await loadProviderSettings();
    const current = providerSettings && providerSettings.provider === "openai" ? "openai" : "anthropic";
    const providers = providerSettings && providerSettings.providers ? providerSettings.providers : {};
    const anth = providers.anthropic || {};
    const openai = providers.openai || {};
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay provider-modal";
    overlay.innerHTML = `
      <div class="modal-card provider-card" role="dialog" aria-modal="true" aria-label="Provider settings">
        <div class="modal-header">
          <div class="modal-title">Provider</div>
          <div class="modal-desc">Choose the default AI provider for new tasks, chat, replay, computer-use, and reflection.</div>
        </div>
        <label class="provider-option">
          <input type="radio" name="provider" value="anthropic" ${current === "anthropic" ? "checked" : ""}>
          <span>
            <strong>Anthropic / Claude Code</strong>
            <small>${escapeHtml(anth.status || "")}</small>
          </span>
        </label>
        <label class="provider-option">
          <input type="radio" name="provider" value="openai" ${current === "openai" ? "checked" : ""}>
          <span>
            <strong>OpenAI / Codex</strong>
            <small>${escapeHtml(openai.status || "")}</small>
          </span>
        </label>
        <label class="provider-key">
          <span>Optional API key</span>
          <input type="password" id="providerApiKey" placeholder="Paste key for selected provider">
        </label>
        <div class="provider-message" id="providerMessage"></div>
        <div class="modal-actions row">
          <button class="modal-btn secondary" id="cancelProviderBtn">Cancel</button>
          <button class="modal-btn primary" id="saveProviderBtn">Save Provider</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const keyInput = overlay.querySelector("#providerApiKey");
    const message = overlay.querySelector("#providerMessage");
    const saveBtn = overlay.querySelector("#saveProviderBtn");
    overlay.querySelector("#cancelProviderBtn").addEventListener("click", closeProviderModal);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) closeProviderModal();
    });
    saveBtn.addEventListener("click", async () => {
      const selected = overlay.querySelector('input[name="provider"]:checked');
      const provider = selected ? selected.value : "anthropic";
      const apiKey = keyInput.value.trim();
      message.textContent = "Checking provider...";
      saveBtn.disabled = true;
      try {
        providerSettings = await request("/api/settings/provider", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider, api_key: apiKey || null }),
        });
        renderProviderButton();
        closeProviderModal();
        await loadTasks();
      } catch (e) {
        message.textContent = e.message || String(e);
        saveBtn.disabled = false;
      }
    });
    const checked = overlay.querySelector('input[name="provider"]:checked');
    if (checked) checked.focus();
  }

  function positionOpenMenu() {
    const menu = el.taskList.querySelector(".menu.open");
    if (!menu) return;
    menu.classList.remove("flip-up");
    const rect = menu.getBoundingClientRect();
    if (rect.bottom > window.innerHeight - 8) menu.classList.add("flip-up");
  }

  el.taskList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled) return;
    const row = button.closest(".task-row");
    if (!row) return;
    if (button.dataset.action === "menu") {
      openMenuTaskId = openMenuTaskId === row.dataset.id ? null : row.dataset.id;
      render();
      positionOpenMenu();
      return;
    }
    openMenuTaskId = null;
    runAction(row.dataset.id, button.dataset.action);
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".overflow") && openMenuTaskId !== null) {
      openMenuTaskId = null;
      render();
    }
  });

  el.refreshBtn.addEventListener("click", loadTasks);
  el.providerBtn.addEventListener("click", openProviderModal);
  el.directBuildBtn.addEventListener("click", openDirectBuildModal);
  el.uploadSkillBtn.addEventListener("click", openUploadSkillPicker);
  el.exploreMarketplaceBtn.addEventListener("click", () => {
    window.location.href = "/marketplace";
  });
  el.agentModeBtn.addEventListener("click", () => {
    window.location.href = "/agent";
  });
  el.openWorkflowsBtn.addEventListener("click", async () => {
    try {
      await request("/api/app/open-workflows", { method: "POST" });
    } catch (e) {
      alert(e.message || String(e));
    }
  });
  el.quitAppBtn.addEventListener("click", async () => {
    if (!confirm("Are you sure you want to quit the application and close all processes?")) return;
    try {
      await request("/api/app/quit", { method: "POST" });
      document.body.innerHTML = `
        <div style="min-height: 100vh; display: flex; align-items: center; justify-content: center; background: #0b0f14; color: #e8eef5; font-family: sans-serif; flex-direction: column; gap: 10px;">
          <h2 style="margin: 0; font-weight: 700;">Quitting Application</h2>
          <p style="color: #9baabb; margin: 0;">You can close this tab now.</p>
        </div>
      `;
    } catch (e) {
      alert(e.message || String(e));
    }
  });
  el.startRecordingBtn.addEventListener("click", async () => {
    try {
      el.startRecordingBtn.disabled = true;
      el.startRecordingBtn.textContent = "Opening recorder...";
      await request("/api/recording/start", { method: "POST" });
      await loadTasks();
    } catch (e) {
      alert(e.message || String(e));
      await loadTasks();
    }
  });
  loadProviderSettings();
  loadTasks();
  window.setInterval(loadTasks, 1600);
})();
