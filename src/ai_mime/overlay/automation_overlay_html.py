from __future__ import annotations

AUTOMATION_OVERLAY_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  :root {
    --bg-color: rgba(30, 30, 30, 0.85);
    --border-color: rgba(255, 255, 255, 0.2);
    --text-primary: #ffffff;
    --text-secondary: rgba(255, 255, 255, 0.6);
    --btn-bg: rgba(255, 255, 255, 0.1);
    --btn-hover: rgba(255, 255, 255, 0.2);
    --btn-active: rgba(255, 255, 255, 0.3);
    --accent: #34c759; /* macOS Green */
  }

  @media (prefers-color-scheme: light) {
    :root {
      --bg-color: rgba(255, 255, 255, 0.85);
      --border-color: rgba(0, 0, 0, 0.2);
      --text-primary: #000000;
      --text-secondary: rgba(0, 0, 0, 0.6);
      --btn-bg: rgba(0, 0, 0, 0.05);
      --btn-hover: rgba(0, 0, 0, 0.1);
      --btn-active: rgba(0, 0, 0, 0.15);
    }
  }

  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background-color: transparent;
    color: var(--text-primary);
    overflow: hidden;
    font-size: 13px;
    -webkit-font-smoothing: antialiased;
    user-select: none;
  }

  .container {
    background-color: var(--bg-color);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: none;
    border-radius: 12px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  /* macOS Controls */
  .mac-controls {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .mac-btn {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
  }
  .mac-btn svg {
    opacity: 0;
    transition: opacity 0.1s;
    color: rgba(0, 0, 0, 0.6);
  }
  .mac-controls:hover .mac-btn svg {
    opacity: 1;
  }
  .mac-minimize {
    background-color: #FFBD2E;
    border: 0.5px solid #DEA123;
  }

  /* Header */
  .header {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 600;
  }

  .status-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
  }

  @keyframes spin {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
  }

  .icon-spin {
    animation: spin 1.5s linear infinite;
  }

  .title {
    flex-grow: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Content area */
  .content {
    display: flex;
    flex-direction: column;
    gap: 4px;
    transition: all 0.2s ease;
  }

  .log-line {
    display: -webkit-box;
    -webkit-line-clamp: 4;
    -webkit-box-orient: vertical;
    overflow: hidden;
    text-overflow: ellipsis;
    user-select: text;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text-secondary);
  }

  /* Actions */
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 6px;
    margin-top: 4px;
  }

  button {
    background-color: var(--btn-bg);
    border: none;
    border-radius: 6px;
    padding: 4px 10px;
    color: var(--text-primary);
    font-family: inherit;
    font-size: 11px;
    font-weight: 500;
    cursor: default;
    transition: background-color 0.1s;
    outline: none;
  }

  button:hover {
    background-color: var(--btn-hover);
  }

  button:active {
    background-color: var(--btn-active);
  }

  button:disabled {
    opacity: 0.5;
  }
</style>
</head>
<body>

<div class="container" id="main-container">
  <div class="header" id="header-area">
    <div class="status-icon" id="status-icon">
      <svg class="icon-spin" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: #ff9500;"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line></svg>
    </div>
    <div class="title" id="title-text">AI Mime: Automation</div>
    <div class="mac-controls" id="window-controls">
      <div class="mac-btn mac-minimize" onclick="sendAction('minimize')" title="Minimize">
        <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="12" x2="20" y2="12"></line></svg>
      </div>
    </div>
  </div>

  <div class="content" id="content-area">
    <div class="log-line" id="log-line-text">Waiting for output...</div>
  </div>

  <div class="actions" id="actions-area">
    <button onclick="sendAction('show_chat')">Show Chat</button>
    <button id="stop-btn" onclick="sendAction('interrupt')">Stop</button>
  </div>
</div>

<script>
  let lastHeight = 0;
  let isMinimized = false;

  const svgRunning = `<svg class="icon-spin" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: #ff9500;"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line></svg>`;
  const svgSuccess = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: var(--accent);"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
  const svgFailed = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: #ff3b30;"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>`;

  const resizeObserver = new ResizeObserver(entries => {
    for (let entry of entries) {
      const height = document.documentElement.scrollHeight;
      if (height !== lastHeight && !isMinimized) {
        lastHeight = height;
        if (window.webkit && window.webkit.messageHandlers.overlay) {
          window.webkit.messageHandlers.overlay.postMessage({
            type: 'resize',
            height: height
          });
        }
      }
    }
  });

  resizeObserver.observe(document.body);

  setInterval(() => {
    sendAction('request_state');
  }, 200);

  function sendAction(action, payload) {
    const msg = { type: action };
    if (payload) {
      Object.assign(msg, payload);
    }
    if (window.webkit && window.webkit.messageHandlers.overlay) {
      window.webkit.messageHandlers.overlay.postMessage(msg);
    } else {
      console.log('Action triggered:', action, payload);
    }
  }

  document.getElementById('main-container').addEventListener('mousedown', function(e) {
    if (e.target.tagName.toLowerCase() === 'button' || e.target.closest('.mac-controls')) {
        return;
    }
    if (isMinimized) {
        sendAction('maximize');
    } else {
        sendAction('show_chat');
    }
  });

  function escapeHtml(unsafe) {
      if (!unsafe) return "";
      return unsafe
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#039;");
  }

  function updateOverlayState(stateStr) {
    const state = JSON.parse(stateStr);

    if (state.mode !== undefined) {
      if (state.mode === 'minimized') {
        isMinimized = true;
        document.body.style.padding = '0px';
        document.getElementById('content-area').style.display = 'none';
        document.getElementById('actions-area').style.display = 'none';
        document.getElementById('title-text').style.display = 'none';
        const wc = document.getElementById('window-controls');
        if (wc) wc.style.display = 'none';

        const container = document.getElementById('main-container');
        container.style.width = '32px';
        container.style.height = '32px';
        container.style.padding = '0';
        container.style.borderRadius = '16px';
        container.style.display = 'flex';
        container.style.alignItems = 'center';
        container.style.justifyContent = 'center';
        container.style.border = '1px solid var(--border-color)';
        container.style.backgroundColor = 'var(--bg-color)';
        container.style.boxShadow = 'none';
        container.style.backdropFilter = 'blur(20px)';

        if (window.webkit && window.webkit.messageHandlers.overlay) {
          window.webkit.messageHandlers.overlay.postMessage({ type: 'resize', height: 32 });
        }
      } else if (state.mode === 'maximized') {
        isMinimized = false;
        document.body.style.padding = '16px';
        document.getElementById('content-area').style.display = 'flex';
        document.getElementById('actions-area').style.display = 'flex';
        document.getElementById('title-text').style.display = 'block';
        const wc = document.getElementById('window-controls');
        if (wc) wc.style.display = 'flex';

        const container = document.getElementById('main-container');
        container.style.width = 'auto';
        container.style.height = 'auto';
        container.style.padding = '12px';
        container.style.borderRadius = '12px';
        container.style.display = 'flex';
        container.style.alignItems = 'stretch';
        container.style.justifyContent = 'flex-start';
        container.style.border = 'none';
        container.style.backgroundColor = 'var(--bg-color)';
        container.style.boxShadow = '0 4px 12px rgba(0, 0, 0, 0.15)';
        container.style.backdropFilter = 'blur(20px)';

        lastHeight = 0;
        const height = document.documentElement.scrollHeight;
        if (window.webkit && window.webkit.messageHandlers.overlay) {
          window.webkit.messageHandlers.overlay.postMessage({ type: 'resize', height: height });
        }
      }
    }

    if (state.message !== undefined) {
      document.getElementById('log-line-text').innerHTML = escapeHtml(state.message);
    }

    if (state.status !== undefined) {
      const iconDiv = document.getElementById('status-icon');
      if (state.status === 'success') {
        iconDiv.innerHTML = svgSuccess;
      } else if (state.status === 'failed') {
        iconDiv.innerHTML = svgFailed;
      } else {
        iconDiv.innerHTML = svgRunning;
      }
    }

    if (state.stop_disabled !== undefined) {
      document.getElementById('stop-btn').disabled = state.stop_disabled;
    }
  }
</script>

</body>
</html>
"""
