(() => {
  const shell = document.querySelector(".reflect-shell");
  const taskId = shell && shell.dataset ? shell.dataset.taskId : "";
  if (!taskId) return;

  const el = {
    progressTitle: document.getElementById("progressTitle"),
    progressSubtitle: document.getElementById("progressSubtitle"),
    progressFill: document.getElementById("progressFill"),
    passALabel: document.getElementById("passALabel"),
    passBLabel: document.getElementById("passBLabel"),
    optimizedLabel: document.getElementById("optimizedLabel"),
    startReflectBtn: document.getElementById("startReflectBtn"),
  };

  let polling = false;
  let redirectedToSkillBuild = false;

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
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

  function defaultProgress(task) {
    if (task && task.status === "ready") return { value: 100, label: "Optimized plan", phase: "optimized_plan_complete" };
    if (task && task.status === "compiling") return { value: 8, label: "Compiling", phase: "compiling" };
    if (task && task.status === "reflecting") return { value: 5, label: "Reflecting", phase: "reflecting" };
    return { value: 0, label: "Pending", phase: "pending_reflection" };
  }

  function markStep(labelEl, done) {
    if (!labelEl) return;
    labelEl.classList.toggle("done", !!done);
  }

  function renderStatus(task) {
    const progress = task && task.progress && typeof task.progress === "object"
      ? task.progress
      : defaultProgress(task);
    const value = Math.max(0, Math.min(100, Number(progress.value || 0)));
    const phase = String(progress.phase || task.phase || task.status || "");
    const name = task.display_name || task.id || taskId;
    const label = progress.label || statusLabel(task.status);
    const status = task.status || "unknown";

    document.title = `AI Mime - Reflect - ${name}`;
    if (el.progressTitle) el.progressTitle.textContent = label;
    if (el.progressSubtitle) {
      const error = task.error ? ` - ${task.error}` : "";
      el.progressSubtitle.textContent = `${statusLabel(status)}${error}`;
      el.progressSubtitle.title = task.error || "";
    }
    if (el.progressFill) el.progressFill.style.width = `${value}%`;
    markStep(el.passALabel, value >= 33 || phase === "pass_a_complete");
    markStep(el.passBLabel, value >= 66 || phase === "pass_b_complete");
    markStep(el.optimizedLabel, value >= 100 || phase === "optimized_plan_complete");
    if (!redirectedToSkillBuild && (phase === "optimized_plan_complete" || (status === "ready" && value >= 100))) {
      redirectedToSkillBuild = true;
      window.location.replace(`/skill-build/${encodeURIComponent(taskId)}`);
      return;
    }
    if (el.startReflectBtn) {
      el.startReflectBtn.hidden = !task.can_reflect;
      el.startReflectBtn.textContent = status === "failed_reflection" ? "Retry reflection" : "Start reflection";
    }
  }

  async function loadStatus() {
    try {
      const task = await request(`/api/tasks/${encodeURIComponent(taskId)}/reflect/status`);
      renderStatus(task);
    } catch (e) {
      if (el.progressTitle) el.progressTitle.textContent = "Unable to load reflection";
      if (el.progressSubtitle) el.progressSubtitle.innerHTML = escapeHtml(e.message || String(e));
    }
  }

  async function startReflect() {
    if (!el.startReflectBtn) return;
    el.startReflectBtn.disabled = true;
    try {
      const task = await request(`/api/tasks/${encodeURIComponent(taskId)}/reflect`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: false }),
      });
      renderStatus(task);
    } catch (e) {
      if (el.progressSubtitle) el.progressSubtitle.textContent = e.message || String(e);
    } finally {
      el.startReflectBtn.disabled = false;
    }
  }

  if (el.startReflectBtn) {
    el.startReflectBtn.addEventListener("click", startReflect);
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      await loadStatus();
    } finally {
      polling = false;
    }
  }

  poll();
  window.setInterval(poll, 1200);
})();
