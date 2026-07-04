from __future__ import annotations

AUTOMATION_OVERLAY_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  :root {
    --bg-color: #222222; /* Solid dark background */
    --border-color: rgba(255, 255, 255, 0.1);
    --text-primary: #ffffff;
    --text-secondary: rgba(255, 255, 255, 0.6);
    --btn-bg: rgba(255, 255, 255, 0.05);
    --btn-hover: rgba(255, 255, 255, 0.15);
    --btn-active: rgba(255, 255, 255, 0.2);
    
    --color-running: #ff9500;
    --color-running-bg: rgba(255, 149, 0, 0.15);
    
    --color-completed: #34c759;
    --color-completed-bg: rgba(52, 199, 89, 0.15);
    
    --color-failed: #ff3b30;
    --color-failed-bg: rgba(255, 59, 48, 0.15);

    --shadow-1: rgba(0, 0, 0, 0.2);
    --shadow-2: rgba(0, 0, 0, 0.3);
    --shadow-3: rgba(0, 0, 0, 0.4);
  }

  @media (prefers-color-scheme: light) {
    :root {
      --bg-color: #ffffff; /* Solid light background */
      --border-color: rgba(17, 20, 28, 0.10);
      --text-primary: #000000;
      --text-secondary: rgba(0, 0, 0, 0.6);
      --btn-bg: rgba(0, 0, 0, 0.03);
      --btn-hover: rgba(0, 0, 0, 0.08);
      --btn-active: rgba(0, 0, 0, 0.12);
      
      --shadow-1: rgba(17, 20, 28, 0.06);
      --shadow-2: rgba(17, 20, 28, 0.14);
      --shadow-3: rgba(17, 20, 28, 0.10);
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
    border: 1px solid var(--border-color);
    border-radius: 12px;
    box-shadow:
      0 2px 4px var(--shadow-1),
      0 12px 26px var(--shadow-2),
      0 26px 52px var(--shadow-3);
    padding: 12px 12px 16px 16px; /* slightly more bottom padding, left padding for the rail */
    display: flex;
    flex-direction: column;
    gap: 10px;
    position: relative;
    overflow: hidden;
  }

  /* State Accent Rail */
  .container::before {
    content: ''; 
    position: absolute; 
    left: 0; 
    top: 12px; 
    bottom: 12px;
    width: 3px; 
    border-radius: 0 3px 3px 0; 
    background: var(--state-color, transparent);
    transition: background 0.3s ease;
  }

  /* Header Controls */
  .mac-controls {
    display: flex;
    align-items: center;
    gap: 2px;
  }
  
  .control-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    cursor: pointer;
    color: var(--text-secondary);
    border-radius: 4px;
    transition: background-color 0.1s, color 0.1s;
  }
  .control-btn:hover {
    background-color: var(--btn-hover);
    color: var(--text-primary);
  }
  
  #stop-btn:hover {
    color: var(--color-failed);
    background-color: var(--color-failed-bg);
  }

  .controls-divider {
    width: 1px;
    height: 12px;
    background-color: var(--border-color);
    margin: 0 4px;
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
    color: var(--state-color);
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

  /* Tag Pill */
  .log-tag {
    background-color: var(--state-bg);
    color: var(--state-color);
    padding: 2px 6px;
    border-radius: 4px;
    font-weight: 600;
    font-size: 10.5px;
    text-transform: lowercase;
    vertical-align: middle;
    margin-right: 6px;
    display: inline-block;
  }

  /* Bottom Bar */
  .bottom-bar {
    position: absolute;
    bottom: 0;
    left: 0;
    height: 3px;
    width: 100%;
    background: transparent;
    overflow: hidden;
  }
  
  .bottom-bar::after {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    height: 100%;
    width: 50%;
    background: linear-gradient(90deg, transparent 0%, var(--state-color) 50%, transparent 100%);
    transform: translateX(-100%);
    opacity: 0;
    transition: opacity 0.3s;
  }
  
  @keyframes sweep {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(200%); }
  }

  .bottom-bar.running::after {
    opacity: 1;
    animation: sweep 1.5s infinite linear;
  }
  
  .bottom-bar.completed {
    background: var(--state-color);
  }
  
  .bottom-bar.failed {
    background: var(--state-color);
  }
</style>
</head>
<body>

<div class="container" id="main-container">
  <div class="header" id="header-area">
    <div class="status-icon" id="status-icon">
      <svg class="icon-spin" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: var(--color-running);"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line></svg>
    </div>
    <div class="title" id="title-text">Running</div>
    <div class="mac-controls" id="window-controls">
      <div class="control-btn" id="chat-btn" onclick="sendAction('show_chat')" title="Show Chat">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"></path></svg>
      </div>
      <div class="control-btn" id="stop-btn" onclick="sendAction('interrupt')" title="Stop">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><rect x="2" y="2" width="20" height="20" rx="4" ry="4"></rect></svg>
      </div>
      <div class="controls-divider" id="controls-divider"></div>
      <div class="control-btn" onclick="sendAction('minimize')" title="Minimize">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"></line></svg>
      </div>
    </div>
  </div>

  <div class="content" id="content-area">
    <div class="log-line" id="log-line-text">Waiting for output...</div>
  </div>

  <div class="bottom-bar running" id="bottom-bar"></div>
</div>

<script>
  let lastHeight = 0;
  let isMinimized = false;

  const svgRunning = `<svg class="icon-spin" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line></svg>`;
  const svgSuccess = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
  const svgFailed = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>`;

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
    if (e.target.tagName.toLowerCase() === 'button' || e.target.closest('.mac-controls') || e.target.closest('.control-btn')) {
        return;
    }
    if (isMinimized) {
        sendAction('maximize');
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
    const container = document.getElementById('main-container');

    if (state.mode !== undefined) {
      if (state.mode === 'minimized') {
        isMinimized = true;
        document.body.style.padding = '0px';
        document.getElementById('content-area').style.display = 'none';
        document.getElementById('title-text').style.display = 'none';
        const wc = document.getElementById('window-controls');
        if (wc) wc.style.display = 'none';
        document.getElementById('bottom-bar').style.display = 'none';

        container.style.width = '32px';
        container.style.height = '32px';
        container.style.padding = '0';
        container.style.borderRadius = '16px';
        container.style.display = 'flex';
        container.style.alignItems = 'center';
        container.style.justifyContent = 'center';
        
        // Remove the visual elevation + rail on the minimized circular spinner
        container.style.boxShadow = 'none';
        container.style.border = '1px solid var(--border-color)';
        // A hack to hide the ::before rail pseudo element on minimized by setting transparent color
        container.style.setProperty('--state-color', 'transparent');

        if (window.webkit && window.webkit.messageHandlers.overlay) {
          window.webkit.messageHandlers.overlay.postMessage({ type: 'resize', height: 32 });
        }
      } else if (state.mode === 'maximized') {
        isMinimized = false;
        // Provide enough padding to prevent the large 52px blur shadow from being clipped by the webview bounds
        document.body.style.padding = '24px 32px 64px 32px';
        document.getElementById('content-area').style.display = 'flex';
        document.getElementById('title-text').style.display = 'block';
        const wc = document.getElementById('window-controls');
        if (wc) wc.style.display = 'flex';
        document.getElementById('bottom-bar').style.display = 'block';

        container.style.width = 'auto';
        container.style.height = 'auto';
        container.style.padding = '12px 12px 16px 16px';
        container.style.borderRadius = '12px';
        container.style.display = 'flex';
        container.style.alignItems = 'stretch';
        container.style.justifyContent = 'flex-start';
        
        // Restore styling
        container.style.border = '1px solid var(--border-color)';
        container.style.boxShadow = '0 2px 4px var(--shadow-1), 0 12px 26px var(--shadow-2), 0 26px 52px var(--shadow-3)';

        lastHeight = 0;
        const height = document.documentElement.scrollHeight;
        if (window.webkit && window.webkit.messageHandlers.overlay) {
          window.webkit.messageHandlers.overlay.postMessage({ type: 'resize', height: height });
        }
      }
    }

    if (state.status !== undefined) {
      const iconDiv = document.getElementById('status-icon');
      const titleText = document.getElementById('title-text');
      const bottomBar = document.getElementById('bottom-bar');
      const stopBtn = document.getElementById('stop-btn');
      const divider = document.getElementById('controls-divider');
      
      bottomBar.className = 'bottom-bar'; // reset

      if (state.status === 'success') {
        iconDiv.innerHTML = svgSuccess;
        titleText.textContent = 'Completed';
        
        if (!isMinimized) {
            container.style.setProperty('--state-color', 'var(--color-completed)');
            container.style.setProperty('--state-bg', 'var(--color-completed-bg)');
        }
        
        bottomBar.classList.add('completed');
        if (stopBtn) stopBtn.style.display = 'none';
        if (divider) divider.style.display = 'none';
      } else if (state.status === 'failed') {
        iconDiv.innerHTML = svgFailed;
        titleText.textContent = 'Failed';
        
        if (!isMinimized) {
            container.style.setProperty('--state-color', 'var(--color-failed)');
            container.style.setProperty('--state-bg', 'var(--color-failed-bg)');
        }

        bottomBar.classList.add('failed');
        if (stopBtn) stopBtn.style.display = 'none';
        if (divider) divider.style.display = 'none';
      } else {
        iconDiv.innerHTML = svgRunning;
        titleText.textContent = 'Running';
        
        if (!isMinimized) {
            container.style.setProperty('--state-color', 'var(--color-running)');
            container.style.setProperty('--state-bg', 'var(--color-running-bg)');
        }

        bottomBar.classList.add('running');
        if (stopBtn) stopBtn.style.display = 'flex';
        if (divider) divider.style.display = 'block';
      }
    }

    if (state.message !== undefined) {
      let text = state.message || "";
      const regex = /^\[(.*?)\]\s*/;
      const match = text.match(regex);
      let tagHtml = '';
      
      if (match) {
        tagHtml = `<span class="log-tag">${escapeHtml(match[1])}</span>`;
        text = text.substring(match[0].length);
      }
      
      document.getElementById('log-line-text').innerHTML = tagHtml + escapeHtml(text);
    }

    if (state.stop_disabled !== undefined) {
      const stopBtn = document.getElementById('stop-btn');
      if (stopBtn) {
        if (state.stop_disabled) {
          stopBtn.style.opacity = '0.5';
          stopBtn.style.pointerEvents = 'none';
        } else {
          stopBtn.style.opacity = '1';
          stopBtn.style.pointerEvents = 'auto';
        }
      }
    }
  }
</script>

</body>
</html>
"""
