(() => {
  let WORKFLOW_ID = window.__WORKFLOW_ID__ || "";
  if (!WORKFLOW_ID) {
    // Fallback: derive from URL (/workflows/<id>) so the page works even if
    // template injection fails for any reason.
    const m = String(window.location.pathname || "").match(/\/workflows\/([^/]+)$/);
    if (m && m[1]) WORKFLOW_ID = decodeURIComponent(m[1]);
  }
  const $ = (id) => document.getElementById(id);

  const el = {
    wfTitle: $("wfTitle"),
    wfSub: $("wfSub"),
    dirtyFlag: $("dirtyFlag"),
    errorBanner: $("errorBanner"),
    errorText: $("errorText"),
    saveBtn: $("saveBtn"),
    revertBtn: $("revertBtn"),
    taskName: $("taskName"),
    taskDesc: $("taskDesc"),
    paramsTable: $("paramsTable"),
    addParamBtn: $("addParamBtn"),
    subtaskList: $("subtaskList"),
    subtasksContainer: $("subtasksContainer"),
    modalBackdrop: $("modalBackdrop"),
    depSearch: $("depSearch"),
    depList: $("depList"),
    modalCancel: $("modalCancel"),
    modalSub: $("modalSub"),

    stepModalBackdrop: $("stepModalBackdrop"),
    stepModalSub: $("stepModalSub"),
    stepExpected: $("stepExpected"),
    stepTargetPrimary: $("stepTargetPrimary"),
    stepTargetFallback: $("stepTargetFallback"),
    stepPostAction: $("stepPostAction"),
    stepVariableName: $("stepVariableName"),
    stepExtractQuery: $("stepExtractQuery"),
    stepModalCancel: $("stepModalCancel"),
    stepModalApply: $("stepModalApply"),

    addSubtaskBtn: $("addSubtaskBtn"),

    errorModalBackdrop: $("errorModalBackdrop"),
    errorModalText: $("errorModalText"),
    errorModalClose: $("errorModalClose"),
  };

  const ACTION_TYPES = ["CLICK", "DOUBLE_CLICK", "RIGHT_CLICK", "MIDDLE_CLICK", "TYPE", "SCROLL", "KEYPRESS", "DRAG", "EXTRACT"];

  let originalSchema = null;
  let schema = null;
  let metadata = null;
  let dirty = false;
  let deletedParamExamples = {};

  let depModal = {
    open: false,
    subtask_i: null,
    extracts: [],
  };

  let stepModal = {
    open: false,
    subtask_i: null,
    step_i: null,
  };

  function setDirty(v) {
    dirty = !!v;
    el.dirtyFlag.hidden = !dirty;
  }

  function showError(msg) {
    el.errorText.textContent = String(msg || "");
    el.errorBanner.hidden = false;
    // Also show a modal so errors are visible even when scrolled.
    openErrorModal(msg);
  }

  function clearError() {
    el.errorBanner.hidden = true;
    el.errorText.textContent = "";
  }

  function openErrorModal(msg) {
    if (!el.errorModalBackdrop) return;
    el.errorModalText.textContent = String(msg || "");
    el.errorModalBackdrop.hidden = false;
  }

  function closeErrorModal() {
    if (!el.errorModalBackdrop) return;
    el.errorModalBackdrop.hidden = true;
  }

  function deepCopy(obj) {
    return JSON.parse(JSON.stringify(obj));
  }

  function safeStr(x) {
    return x == null ? "" : String(x);
  }

  async function apiGet(path) {
    const r = await fetch(path, { method: "GET" });
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  }

  async function apiPost(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const text = await r.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      // ignore
    }
    if (!r.ok) {
      const detail = data && data.detail ? data.detail : text;
      throw new Error(detail || `HTTP ${r.status}`);
    }
    return data;
  }

  function ensurePlanShape(s) {
    if (!s || typeof s !== "object") return { plan: { subtasks: [] } };
    if (!s.plan || typeof s.plan !== "object") s.plan = { subtasks: [] };
    if (!Array.isArray(s.plan.subtasks)) s.plan.subtasks = [];
    return s;
  }

  function render() {
    if (!schema) return;
    clearError();

    el.wfTitle.textContent = metadata && metadata.name ? metadata.name : "Workflow Editor";
    el.wfSub.textContent = `${WORKFLOW_ID} • schema.json`;

    el.taskName.value = safeStr(schema.task_name);
    el.taskDesc.value = safeStr(schema.detailed_task_description);

    renderParams();
    renderSubtasks();
    renderSidebar();
  }

  function renderParams() {
    const params = Array.isArray(schema.task_params) ? schema.task_params : [];
    const head = document.createElement("div");
    head.className = "table-row table-head";
    head.innerHTML =
      "<div>Name</div><div>Type</div><div>Description</div><div>Example</div><div>Optional</div><div>Sensitive</div><div></div>";

    const wrap = document.createElement("div");
    wrap.appendChild(head);

    params.forEach((p, idx) => {
      const row = document.createElement("div");
      row.className = "table-row";

      const name = document.createElement("input");
      name.type = "text";
      name.value = safeStr(p.name);
      name.oninput = () => {
        p.name = name.value;
        setDirty(true);
      };

      const type = document.createElement("input");
      type.type = "text";
      type.value = safeStr(p.type || "string");
      type.oninput = () => {
        p.type = type.value;
        setDirty(true);
      };

      const desc = document.createElement("input");
      desc.type = "text";
      desc.value = safeStr(p.description);
      desc.oninput = () => {
        p.description = desc.value;
        setDirty(true);
      };

      const ex = document.createElement("input");
      ex.type = "text";
      ex.value = safeStr(p.example);
      ex.oninput = () => {
        p.example = ex.value;
        setDirty(true);
      };

      const optional = document.createElement("select");
      optional.innerHTML = `<option value="false">false</option><option value="true">true</option>`;
      optional.value = String(!!p.optional);
      optional.onchange = () => {
        p.optional = optional.value === "true";
        setDirty(true);
      };

      const sensitive = document.createElement("select");
      sensitive.innerHTML = `<option value="false">false</option><option value="true">true</option>`;
      sensitive.value = String(!!p.sensitive);
      sensitive.onchange = () => {
        p.sensitive = sensitive.value === "true";
        setDirty(true);
      };

      const delBtn = document.createElement("button");
      delBtn.className = "btn";
      delBtn.textContent = "×";
      delBtn.title = "Delete parameter";
      delBtn.onclick = () => {
        const nameNow = String(p.name || "").trim();
        if (nameNow) {
          const exNow = p.example == null ? "" : String(p.example);
          deletedParamExamples[nameNow] = exNow;
        }
        params.splice(idx, 1);
        schema.task_params = params;
        setDirty(true);
        renderParams();
      };

      row.appendChild(name);
      row.appendChild(type);
      row.appendChild(desc);
      row.appendChild(ex);
      row.appendChild(optional);
      row.appendChild(sensitive);
      row.appendChild(delBtn);

      wrap.appendChild(row);
    });

    el.paramsTable.innerHTML = "";
    el.paramsTable.appendChild(wrap);
  }

  function renderSidebar() {
    const subtasks = schema.plan.subtasks || [];
    el.subtaskList.innerHTML = "";
    subtasks.forEach((st, i) => {
      const item = document.createElement("div");
      item.className = "sidebar-item";
      item.innerHTML = `<div class="k">subtask ${i}</div><div class="t">${escapeHtml(shortText(st.text || ""))}</div>`;
      item.onclick = () => {
        const target = document.querySelector(`[data-subtask-i="${i}"]`);
        if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      };
      el.subtaskList.appendChild(item);
    });
  }

  function shortText(s) {
    const t = String(s || "").replace(/\s+/g, " ").trim();
    if (t.length <= 80) return t;
    return t.slice(0, 77) + "…";
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function renderSubtasks() {
    const subtasks = schema.plan.subtasks || [];
    el.subtasksContainer.innerHTML = "";

    subtasks.forEach((st, si) => {
      const card = document.createElement("div");
      card.className = "subtask-card";
      card.dataset.subtaskI = String(si);

      const header = document.createElement("div");
      header.className = "subtask-header";
      header.innerHTML = `<div class="subtask-title">Subtask ${si}</div>`;

      const headerRight = document.createElement("div");
      headerRight.className = "subtask-actions";

      const addAbove = document.createElement("button");
      addAbove.className = "btn small";
      addAbove.textContent = "+ Above";
      addAbove.title = "Insert subtask above";
      addAbove.onclick = () => addSubtaskAt(si);

      const addBelow = document.createElement("button");
      addBelow.className = "btn small";
      addBelow.textContent = "+ Below";
      addBelow.title = "Insert subtask below";
      addBelow.onclick = () => addSubtaskAt(si + 1);

      const delSub = document.createElement("button");
      delSub.className = "btn small danger";
      delSub.textContent = "Delete";
      delSub.title = "Delete subtask";
      delSub.onclick = () => {
        if (!confirm(`Delete subtask ${si}? This will delete all its steps.`)) return;
        schema.plan.subtasks.splice(si, 1);
        setDirty(true);
        closeDepModal();
        closeStepModal();
        renderSubtasks();
        renderSidebar();
      };

      const meta = document.createElement("div");
      meta.className = "subtask-meta";
      meta.textContent = "dependencies • steps";

      headerRight.appendChild(addAbove);
      headerRight.appendChild(addBelow);
      headerRight.appendChild(delSub);
      headerRight.appendChild(meta);

      header.appendChild(headerRight);
      card.appendChild(header);

      const textField = document.createElement("label");
      textField.className = "field";
      textField.innerHTML = `<div class="label">Subtask text</div>`;
      const ta = document.createElement("textarea");
      ta.rows = 3;
      ta.id = `subtaskText-${si}`;
      ta.value = safeStr(st.text);
      ta.oninput = () => {
        st.text = ta.value;
        setDirty(true);
        renderSidebar();
      };
      textField.appendChild(ta);
      card.appendChild(textField);

      // Dependencies
      const deps = Array.isArray(st.dependencies) ? st.dependencies : [];
      st.dependencies = deps;

      const depsLabel = document.createElement("div");
      depsLabel.className = "label";
      depsLabel.textContent = "Dependencies (upstream extract variables)";
      card.appendChild(depsLabel);

      const depsRow = document.createElement("div");
      depsRow.className = "deps-row";
      deps.forEach((d) => {
        const pill = document.createElement("span");
        pill.className = "pill";
        pill.innerHTML = `<span>${escapeHtml(d)}</span>`;
        const x = document.createElement("button");
        x.textContent = "×";
        x.title = "Remove dependency";
        x.onclick = () => {
          st.dependencies = deps.filter((x) => x !== d);
          setDirty(true);
          renderSubtasks();
        };
        pill.appendChild(x);
        depsRow.appendChild(pill);
      });

      const addDepBtn = document.createElement("button");
      addDepBtn.className = "btn";
      addDepBtn.textContent = "+ Add dependency";
      addDepBtn.onclick = () => openDepModal(si);
      depsRow.appendChild(addDepBtn);
      card.appendChild(depsRow);

      // Steps
      const stepsWrap = document.createElement("div");
      stepsWrap.className = "steps";
      const steps = Array.isArray(st.steps) ? st.steps : [];
      st.steps = steps;

      const stepsHead = document.createElement("div");
      stepsHead.className = "hint";
      stepsHead.textContent = "Edit step intent, action type, and action value.";
      stepsWrap.appendChild(stepsHead);

      steps.forEach((step, li) => {
        const row = document.createElement("div");
        row.className = "step-row";

        const idx = document.createElement("div");
        idx.className = "step-i";
        idx.textContent = String(li);

        const intent = document.createElement("textarea");
        intent.rows = 2;
        intent.value = safeStr(step.intent);
        intent.oninput = () => {
          step.intent = intent.value;
          setDirty(true);
        };

        const at = document.createElement("select");
        at.innerHTML = ACTION_TYPES.map((x) => `<option value="${x}">${x}</option>`).join("");
        at.value = safeStr(step.action_type || "CLICK");
        at.onchange = () => {
          step.action_type = at.value;
          // Enforce action_value rules for schema validity.
          if (at.value === "TYPE" || at.value === "KEYPRESS") {
            // Allow null; do not force a value.
          } else if (at.value === "EXTRACT") {
            // EXTRACT action_value must equal variable_name; we don't edit variable_name in this UI.
            if (step.variable_name) step.action_value = step.variable_name;
          } else {
            step.action_value = null;
          }
          setDirty(true);
          renderSubtasks();
        };

        const av = document.createElement("input");
        av.type = "text";
        const actionType = at.value;
        const editable = actionType === "TYPE" || actionType === "KEYPRESS";
        av.disabled = !editable;
        av.value = editable ? safeStr(step.action_value) : safeStr(step.action_value);
        av.placeholder = editable ? "action_value" : "null";
        av.oninput = () => {
          // Allow null action_value for TYPE/KEYPRESS: treat empty input as null.
          step.action_value = av.value === "" ? null : av.value;
          setDirty(true);
        };

        const actions = document.createElement("div");
        actions.className = "step-actions";

        const detailsBtn = document.createElement("button");
        detailsBtn.className = "btn small";
        detailsBtn.textContent = "Details";
        detailsBtn.onclick = () => openStepModal(si, li);

        const delBtn = document.createElement("button");
        delBtn.className = "btn small danger";
        delBtn.textContent = "Delete";
        delBtn.onclick = () => {
          const st2 = schema.plan.subtasks[si];
          if (!st2 || !Array.isArray(st2.steps)) return;
          st2.steps.splice(li, 1);
          setDirty(true);
          renderSubtasks();
        };

        actions.appendChild(detailsBtn);
        actions.appendChild(delBtn);

        row.appendChild(idx);
        row.appendChild(intent);
        row.appendChild(at);
        row.appendChild(av);
        row.appendChild(actions);

        stepsWrap.appendChild(row);
      });

      card.appendChild(stepsWrap);
      el.subtasksContainer.appendChild(card);
    });
  }

  async function openDepModal(subtask_i) {
    depModal.open = true;
    depModal.subtask_i = subtask_i;
    el.depSearch.value = "";
    el.depList.innerHTML = "Loading…";
    el.modalSub.textContent = `Choose an upstream extract variable for subtask ${subtask_i}`;
    el.modalBackdrop.hidden = false;
    el.depSearch.focus();

    try {
      const data = await apiGet(`/api/workflows/${encodeURIComponent(WORKFLOW_ID)}/upstream_extracts?subtask_i=${subtask_i}`);
      depModal.extracts = Array.isArray(data.extracts) ? data.extracts : [];
      renderDepModalList();
    } catch (e) {
      depModal.extracts = [];
      el.depList.innerHTML = `<div class="modal-item">Failed to load extracts: ${escapeHtml(e.message || String(e))}</div>`;
    }
  }

  function closeDepModal() {
    depModal.open = false;
    depModal.subtask_i = null;
    depModal.extracts = [];
    el.modalBackdrop.hidden = true;
  }

  function renderDepModalList() {
    const q = (el.depSearch.value || "").trim().toLowerCase();
    const items = depModal.extracts.filter((x) => !q || String(x).toLowerCase().includes(q));
    el.depList.innerHTML = "";
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "modal-item";
      empty.textContent = "No upstream extracts available.";
      el.depList.appendChild(empty);
      return;
    }
    items.forEach((name) => {
      const it = document.createElement("div");
      it.className = "modal-item";
      it.textContent = name;
      it.onclick = () => {
        const st = schema.plan.subtasks[depModal.subtask_i];
        const deps = Array.isArray(st.dependencies) ? st.dependencies : [];
        if (!deps.includes(name)) deps.push(name);
        st.dependencies = deps;
        setDirty(true);
        closeDepModal();
        renderSubtasks();
      };
      el.depList.appendChild(it);
    });
  }

  async function load() {
    if (!WORKFLOW_ID) {
      showError("Missing workflow id");
      return;
    }
    el.saveBtn.disabled = true;
    el.revertBtn.disabled = true;
    try {
      const data = await apiGet(`/api/workflows/${encodeURIComponent(WORKFLOW_ID)}`);
      metadata = data.metadata || {};
      schema = ensurePlanShape(deepCopy(data.schema || {}));
      originalSchema = deepCopy(schema);
      deletedParamExamples = {};
      setDirty(false);
      render();
    } catch (e) {
      showError(e.message || String(e));
    } finally {
      el.saveBtn.disabled = false;
      el.revertBtn.disabled = false;
    }
  }

  async function save() {
    if (!schema) return;
    clearError();
    el.saveBtn.disabled = true;
    try {
      const res = await apiPost(`/api/workflows/${encodeURIComponent(WORKFLOW_ID)}`, {
        schema,
        deleted_param_examples: deletedParamExamples,
      });
      schema = ensurePlanShape(deepCopy(res.schema || schema));
      originalSchema = deepCopy(schema);
      deletedParamExamples = {};
      setDirty(false);
      render();
    } catch (e) {
      showError(e.message || String(e));
    } finally {
      el.saveBtn.disabled = false;
    }
  }

  function revert() {
    if (!originalSchema) return;
    schema = deepCopy(originalSchema);
    deletedParamExamples = {};
    setDirty(false);
    render();
  }

  // Wire inputs
  el.taskName.addEventListener("input", () => {
    if (!schema) return;
    schema.task_name = el.taskName.value;
    setDirty(true);
  });
  el.taskDesc.addEventListener("input", () => {
    if (!schema) return;
    schema.detailed_task_description = el.taskDesc.value;
    setDirty(true);
  });
  el.addParamBtn.addEventListener("click", () => {
    if (!schema) return;
    const params = Array.isArray(schema.task_params) ? schema.task_params : [];
    params.push({
      name: "",
      type: "string",
      description: "",
      example: "",
      sensitive: false,
      optional: false,
    });
    schema.task_params = params;
    setDirty(true);
    renderParams();
  });

  function addSubtaskAt(index) {
    if (!schema) return;
    const subtasks = Array.isArray(schema.plan.subtasks) ? schema.plan.subtasks : [];
    const insertAt = Math.max(0, Math.min(index, subtasks.length));
    subtasks.splice(insertAt, 0, { subtask_i: insertAt, text: "", dependencies: [], steps: [] });
    schema.plan.subtasks = subtasks;
    setDirty(true);
    renderSubtasks();
    renderSidebar();
    // Focus the new subtask text area
    setTimeout(() => {
      const ta = document.getElementById(`subtaskText-${insertAt}`);
      if (ta) ta.focus();
    }, 0);
  }

  el.addSubtaskBtn.addEventListener("click", () => addSubtaskAt((schema && schema.plan && schema.plan.subtasks ? schema.plan.subtasks.length : 0)));

  el.saveBtn.addEventListener("click", save);
  el.revertBtn.addEventListener("click", () => load());

  el.modalCancel.addEventListener("click", closeDepModal);
  el.modalBackdrop.addEventListener("click", (e) => {
    if (e.target === el.modalBackdrop) closeDepModal();
  });
  el.depSearch.addEventListener("input", renderDepModalList);

  // Step details modal
  function openStepModal(subtask_i, step_i) {
    stepModal.open = true;
    stepModal.subtask_i = subtask_i;
    stepModal.step_i = step_i;

    const st = schema.plan.subtasks[subtask_i];
    const step = st && Array.isArray(st.steps) ? st.steps[step_i] : null;
    if (!step || typeof step !== "object") return;

    el.stepModalSub.textContent = `Subtask ${subtask_i}, step ${step_i}`;

    el.stepExpected.value = safeStr(step.expected_current_state);
    const target = step.target && typeof step.target === "object" ? step.target : {};
    el.stepTargetPrimary.value = safeStr(target.primary);
    el.stepTargetFallback.value = safeStr(target.fallback);

    const pa = Array.isArray(step.post_action) ? step.post_action : [];
    el.stepPostAction.value = pa.map((x) => String(x)).join("\n");

    el.stepVariableName.value = safeStr(step.variable_name);
    const aa = step.additional_args && typeof step.additional_args === "object" ? step.additional_args : {};
    el.stepExtractQuery.value = safeStr(aa.extract_query);

    const isExtract = String(step.action_type || "") === "EXTRACT";
    el.stepVariableName.disabled = !isExtract;
    el.stepExtractQuery.disabled = !isExtract;

    el.stepModalBackdrop.hidden = false;
  }

  function closeStepModal() {
    stepModal.open = false;
    stepModal.subtask_i = null;
    stepModal.step_i = null;
    el.stepModalBackdrop.hidden = true;
  }

  function applyStepModal() {
    const subtask_i = stepModal.subtask_i;
    const step_i = stepModal.step_i;
    const st = schema.plan.subtasks[subtask_i];
    const step = st && Array.isArray(st.steps) ? st.steps[step_i] : null;
    if (!step || typeof step !== "object") return;

    step.expected_current_state = el.stepExpected.value;
    const target = step.target && typeof step.target === "object" ? step.target : {};
    target.primary = el.stepTargetPrimary.value;
    const fb = el.stepTargetFallback.value.trim();
    target.fallback = fb ? fb : null;
    step.target = target;

    const lines = String(el.stepPostAction.value || "")
      .split("\n")
      .map((x) => x.trim())
      .filter(Boolean);
    step.post_action = lines.length ? lines : step.post_action;

    const isExtract = String(step.action_type || "") === "EXTRACT";
    if (isExtract) {
      step.variable_name = el.stepVariableName.value.trim() || step.variable_name;
      step.action_value = step.variable_name;
      const aa = step.additional_args && typeof step.additional_args === "object" ? step.additional_args : {};
      aa.extract_query = el.stepExtractQuery.value;
      step.additional_args = aa;
    }

    setDirty(true);
    closeStepModal();
    renderSubtasks();
  }

  el.stepModalCancel.addEventListener("click", closeStepModal);
  el.stepModalApply.addEventListener("click", applyStepModal);
  el.stepModalBackdrop.addEventListener("click", (e) => {
    if (e.target === el.stepModalBackdrop) closeStepModal();
  });

  if (el.errorModalClose) el.errorModalClose.addEventListener("click", closeErrorModal);
  if (el.errorModalBackdrop) {
    el.errorModalBackdrop.addEventListener("click", (e) => {
      if (e.target === el.errorModalBackdrop) closeErrorModal();
    });
  }

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !el.modalBackdrop.hidden) closeDepModal();
    if (e.key === "Escape" && !el.stepModalBackdrop.hidden) closeStepModal();
    if (e.key === "Escape" && el.errorModalBackdrop && !el.errorModalBackdrop.hidden) closeErrorModal();
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      save();
    }
  });

  load();
})();
