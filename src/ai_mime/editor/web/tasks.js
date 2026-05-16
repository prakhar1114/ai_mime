(() => {
  const el = {
    taskList: document.getElementById("taskList"),
    refreshBtn: document.getElementById("refreshBtn"),
    startRecordingBtn: document.getElementById("startRecordingBtn"),
    agentModeBtn: document.getElementById("agentModeBtn"),
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
      const reflectText = task.status === "failed_reflection" ? "Retry reflection" : "Reflect";
      const reflectDisabled = ACTIVE.has(task.status) ? "disabled" : "";
      const menuItems = [
        // task.can_edit ? `<button class="menu-item" data-action="edit">Edit</button>` : "",
        task.can_reflect ? `<button class="menu-item" data-action="reflect" ${reflectDisabled}>${reflectText}</button>` : "",
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
    const encoded = encodeURIComponent(taskId);
    try {
      if (action === "edit") {
        window.location.href = `/workflows/${encoded}`;
        return;
      }
      if (action === "delete") {
        if (!confirm(`Delete ${taskId}? This removes the workflow and recording folders when present.`)) return;
        await request(`/api/tasks/${encoded}`, { method: "DELETE" });
      } else if (action === "reflect") {
        await request(`/api/tasks/${encoded}/reflect`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ force: true }),
        });
        window.location.href = `/reflect/${encoded}`;
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
  el.agentModeBtn.addEventListener("click", () => {
    window.location.href = "/agent";
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
  loadTasks();
  window.setInterval(loadTasks, 1600);
})();
