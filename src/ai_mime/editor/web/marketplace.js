(() => {
  const el = {
    state: document.getElementById("marketplaceState"),
    list: document.getElementById("marketplaceList"),
    search: document.getElementById("marketplaceSearch"),
    refreshBtn: document.getElementById("refreshMarketplaceBtn"),
    backBtn: document.getElementById("backToTasksBtn"),
  };

  let items = [];
  let installingId = null;

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
      // Keep raw text for error reporting.
    }
    if (!res.ok) {
      const detail = data && data.detail ? data.detail : text || `HTTP ${res.status}`;
      throw new Error(detail);
    }
    return data;
  }

  function render() {
    const query = el.search.value.trim().toLowerCase();
    const filtered = items.filter((item) => {
      const haystack = [
        item.name,
        item.description,
        item.type,
        item.author,
        ...(item.tags || []),
      ].join(" ").toLowerCase();
      return !query || haystack.includes(query);
    });

    if (!filtered.length) {
      el.list.innerHTML = `<div class="empty">No marketplace skills match this search.</div>`;
      return;
    }

    el.list.innerHTML = filtered.map((item) => {
      const tags = (item.tags || []).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
      const icon = item.icon_url ? escapeHtml(item.icon_url) : "/static/icon_128.png";
      const isInstalling = installingId === item.id;
      return `
        <article class="marketplace-row" data-id="${escapeHtml(item.id)}">
          <img class="marketplace-icon" src="${icon}" alt="">
          <div class="marketplace-main">
            <div class="marketplace-title-line">
              <h2>
                <a href="https://github.com/prakhar1114/ai_mime_marketplace/tree/main/${(item.github_folder_path || item.id).split('/').map(encodeURIComponent).join('/')}" target="_blank" rel="noopener noreferrer" class="marketplace-item-link">
                  ${escapeHtml(item.name)}
                </a>
              </h2>
              <span class="marketplace-type">${escapeHtml(item.type || "skill")}</span>
            </div>
            <p class="marketplace-description">${escapeHtml(item.description || "")}</p>
            ${tags ? `<div class="marketplace-tags">${tags}</div>` : ""}
            <div class="marketplace-message" data-message></div>
          </div>
          <div class="marketplace-actions">
            <button class="btn primary" data-action="install" ${installingId && !isInstalling ? "disabled" : ""}>
              ${isInstalling ? "Installing..." : "Install"}
            </button>
          </div>
        </article>
      `;
    }).join("");
  }

  function rowForItem(itemId) {
    return Array.from(el.list.querySelectorAll(".marketplace-row")).find((row) => row.dataset.id === itemId) || null;
  }

  async function loadMarketplace() {
    el.state.textContent = "Loading";
    try {
      const data = await request("/api/marketplace/manifest");
      items = Array.isArray(data.items) ? data.items : [];
      render();
      el.state.textContent = `${items.length} ${items.length === 1 ? 'skill' : 'skills'}`;
    } catch (e) {
      el.list.innerHTML = `<div class="empty">Failed to load marketplace: ${escapeHtml(e.message || String(e))}</div>`;
      el.state.textContent = "Error";
    }
  }

  function closeCredsModal() {
    const existing = document.querySelector(".marketplace-creds-modal");
    if (existing) existing.remove();
  }

  function collectCredentials(overlay) {
    const creds = {};
    overlay.querySelectorAll(".cred-input").forEach((input) => {
      const service = input.dataset.credService;
      const key = input.dataset.credKey;
      if (!service || !key) return;
      (creds[service] = creds[service] || {})[key] = input.value;
    });
    return creds;
  }

  function credentialsComplete(overlay) {
    return Array.from(overlay.querySelectorAll(".cred-input")).every(
      (input) => input.value.trim() !== ""
    );
  }

  async function installStaged(stagingId, credentials, onError) {
    try {
      await request("/api/import/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ staging_id: stagingId, credentials: credentials || {} }),
      });
      window.location.href = "/tasks";
    } catch (e) {
      onError(e.message || String(e));
    }
  }

  function showCredentialsModal(data) {
    closeCredsModal();
    const fields = Array.isArray(data.credentials_fields) ? data.credentials_fields : [];
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay marketplace-creds-modal";
    overlay.innerHTML = `
      <div class="modal-card" role="dialog" aria-modal="true" aria-label="Skill credentials">
        <div class="modal-title">${escapeHtml(data.display_name || data.skill_name || "Install skill")}</div>
        <div class="modal-desc">This skill needs your own credentials to run.</div>
        ${fields.map((f, i) => `
          <label class="modal-field">
            <span>${escapeHtml(f.service)} — ${escapeHtml(f.description || f.key)}</span>
            <input type="password" class="cred-input" data-cred-index="${i}"
              data-cred-service="${escapeHtml(f.service)}" data-cred-key="${escapeHtml(f.key)}"
              value="${escapeHtml(f.value || "")}" placeholder="${escapeHtml(f.key)}">
          </label>`).join("")}
        <div class="modal-message" data-modal-message></div>
        <div class="modal-actions">
          <button class="btn" id="cancelCredsBtn">Cancel</button>
          <button class="btn primary" id="confirmCredsBtn" disabled>Install</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const message = overlay.querySelector("[data-modal-message]");
    const confirmBtn = overlay.querySelector("#confirmCredsBtn");
    const refresh = () => { confirmBtn.disabled = !credentialsComplete(overlay); };
    refresh();
    overlay.querySelectorAll(".cred-input").forEach((input) => input.addEventListener("input", refresh));
    overlay.addEventListener("click", (event) => { if (event.target === overlay) closeCredsModal(); });
    overlay.querySelector("#cancelCredsBtn").addEventListener("click", closeCredsModal);
    confirmBtn.addEventListener("click", () => {
      confirmBtn.disabled = true;
      message.textContent = "Installing...";
      installStaged(data.staging_id, collectCredentials(overlay), (err) => {
        message.textContent = err;
        refresh();
      });
    });
  }

  async function installItem(itemId, row) {
    const message = row.querySelector("[data-message]");
    installingId = itemId;
    render();
    const updatedRow = rowForItem(itemId);
    const updatedMessage = updatedRow ? updatedRow.querySelector("[data-message]") : message;
    if (updatedMessage) updatedMessage.textContent = "Downloading and validating skill...";
    try {
      const data = await request("/api/marketplace/stage", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_id: itemId }),
      });
      const fields = data && Array.isArray(data.credentials_fields) ? data.credentials_fields : [];
      if (fields.length) {
        installingId = null;
        render();
        showCredentialsModal(data);
        return;
      }
      await installStaged(data.staging_id, {}, (err) => { throw new Error(err); });
    } catch (e) {
      installingId = null;
      render();
      const failedRow = rowForItem(itemId);
      const failedMessage = failedRow ? failedRow.querySelector("[data-message]") : null;
      if (failedMessage) failedMessage.textContent = e.message || String(e);
    }
  }

  el.list.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action='install']");
    if (!button || button.disabled) return;
    const row = button.closest(".marketplace-row");
    if (!row) return;
    installItem(row.dataset.id, row);
  });

  el.search.addEventListener("input", render);
  el.refreshBtn.addEventListener("click", loadMarketplace);
  el.backBtn.addEventListener("click", () => {
    window.location.href = "/tasks";
  });

  loadMarketplace();
})();
