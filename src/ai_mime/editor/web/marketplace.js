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
      el.list.innerHTML = `<div class="empty">No marketplace workflows match this search.</div>`;
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
              <h2>${escapeHtml(item.name)}</h2>
              <span class="marketplace-type">${escapeHtml(item.type || "workflow")}</span>
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
      el.state.textContent = `${items.length} workflows`;
    } catch (e) {
      el.list.innerHTML = `<div class="empty">Failed to load marketplace: ${escapeHtml(e.message || String(e))}</div>`;
      el.state.textContent = "Error";
    }
  }

  async function installItem(itemId, row) {
    const message = row.querySelector("[data-message]");
    installingId = itemId;
    render();
    const updatedRow = rowForItem(itemId);
    const updatedMessage = updatedRow ? updatedRow.querySelector("[data-message]") : message;
    if (updatedMessage) updatedMessage.textContent = "Downloading and validating skill...";
    try {
      await request("/api/marketplace/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_id: itemId }),
      });
      window.location.href = "/tasks";
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
