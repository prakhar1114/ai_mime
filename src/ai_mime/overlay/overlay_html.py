# overlay_html.py
from __future__ import annotations

OVERLAY_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  :root {
    --bg-color: rgba(30, 30, 30, 0.4);
    --border-color: rgba(255, 255, 255, 0.1);
    --text-primary: #ffffff;
    --text-secondary: rgba(255, 255, 255, 0.6);
    --btn-bg: rgba(255, 255, 255, 0.1);
    --btn-hover: rgba(255, 255, 255, 0.2);
    --btn-active: rgba(255, 255, 255, 0.3);
    --accent: #34c759; /* macOS Green */
  }

  @media (prefers-color-scheme: light) {
    :root {
      --bg-color: rgba(255, 255, 255, 0.4);
      --border-color: rgba(0, 0, 0, 0.1);
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
    overflow: hidden; /* No scrollbars on body, container expands natively */
    font-size: 13px;
    -webkit-font-smoothing: antialiased;
    user-select: none; /* Make it feel like a native app */
  }

  .container {
    background-color: var(--bg-color);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    /* This allows the container to naturally push its height, which we will read in JS */
  }

  /* Header */
  .header {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 600;
  }

  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background-color: var(--accent);
    box-shadow: 0 0 8px var(--accent);
    animation: pulse 2s infinite ease-in-out;
  }

  @keyframes pulse {
    0% { transform: scale(0.95); opacity: 0.5; }
    50% { transform: scale(1.05); opacity: 1; }
    100% { transform: scale(0.95); opacity: 0.5; }
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

  .message {
    line-height: 1.4;
    word-wrap: break-word;
    user-select: text; /* Allow copying text */
  }

  .message p {
    margin-bottom: 6px;
  }
  .message p:last-child {
    margin-bottom: 0;
  }
  .message code {
    background: rgba(127, 127, 127, 0.2);
    padding: 1px 4px;
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 11px;
  }

  .tool-label {
    font-size: 11px;
    color: var(--text-secondary);
    font-weight: 500;
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
<!-- Lightweight markdown renderer -->
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
</head>
<body>

<div class="container" id="main-container">
  <div class="header" id="header-area">
    <div class="dot" id="status-dot"></div>
    <div class="title" id="title-text">AI Agent</div>
  </div>

  <div class="content" id="content-area">
    <div class="message" id="message-text">Initializing...</div>
    <div class="tool-label" id="tool-text"></div>
  </div>

  <div class="actions" id="actions-area">
    <button onclick="sendAction('hide')">Hide</button>
    <button onclick="sendAction('show_chat')">Show Chat</button>
    <button id="interrupt-btn" onclick="sendAction('interrupt')">Interrupt</button>
  </div>
</div>

<script>
  let lastHeight = 0;
  let isMinimized = false;

  // Initialize marked options
  marked.use({
    breaks: true, // Convert \\n to <br>
    gfm: true
  });

  // Observe resizing to notify macOS window
  const resizeObserver = new ResizeObserver(entries => {
    for (let entry of entries) {
      // Document height
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

  // Observe body changes
  resizeObserver.observe(document.body);

  function sendAction(action) {
    if (window.webkit && window.webkit.messageHandlers.overlay) {
      window.webkit.messageHandlers.overlay.postMessage({ type: action });
    } else {
      console.log('Action triggered:', action);
    }
  }

  function handleHeaderClick() {
      if (isMinimized) {
          sendAction('maximize');
      }
  }

  document.getElementById('header-area').addEventListener('mousedown', handleHeaderClick);

  // Exposed function for Python to call
  function updateOverlayState(stateStr) {
    const state = JSON.parse(stateStr);
    
    if (state.title !== undefined) {
      document.getElementById('title-text').textContent = state.title;
    }
    
    if (state.mode !== undefined) {
      if (state.mode === 'minimized') {
        isMinimized = true;
        document.getElementById('content-area').style.display = 'none';
        document.getElementById('actions-area').style.display = 'none';
        document.getElementById('title-text').style.display = 'none';
        
        // Style as a small vertical pill
        const container = document.getElementById('main-container');
        container.style.width = '32px';
        container.style.height = '64px';
        container.style.padding = '0';
        container.style.borderRadius = '16px';
        container.style.display = 'flex';
        container.style.alignItems = 'center';
        container.style.justifyContent = 'center';
        // Keep the background and glassmorphism intact
        container.style.border = '1px solid var(--border-color)';
        container.style.backgroundColor = 'var(--bg-color)';
        container.style.backdropFilter = 'blur(20px)';
        
        // Notify small resize for minimized pill
        if (window.webkit && window.webkit.messageHandlers.overlay) {
          window.webkit.messageHandlers.overlay.postMessage({ type: 'resize', height: 64 });
        }
      } else {
        isMinimized = false;
        document.getElementById('content-area').style.display = 'flex';
        document.getElementById('actions-area').style.display = 'flex';
        document.getElementById('title-text').style.display = 'block';
        
        const container = document.getElementById('main-container');
        container.style.width = 'auto';
        container.style.height = 'auto';
        container.style.padding = '12px';
        container.style.borderRadius = '12px';
        container.style.display = 'flex';
        container.style.alignItems = 'stretch';
        container.style.justifyContent = 'flex-start';
        container.style.border = '1px solid var(--border-color)';
        container.style.backgroundColor = 'var(--bg-color)';
        container.style.backdropFilter = 'blur(20px)';
        
        lastHeight = 0; // force resize event
        // triggering a resize explicitly
        const height = document.documentElement.scrollHeight;
        if (window.webkit && window.webkit.messageHandlers.overlay) {
          window.webkit.messageHandlers.overlay.postMessage({ type: 'resize', height: height });
        }
      }
    }

    if (state.message !== undefined) {
      const msgDiv = document.getElementById('message-text');
      if (state.message.trim() === '') {
        msgDiv.style.display = 'none';
      } else {
        msgDiv.style.display = 'block';
        msgDiv.innerHTML = marked.parse(state.message);
      }
    }

    if (state.tool !== undefined) {
      const toolDiv = document.getElementById('tool-text');
      if (state.tool.trim() === '') {
        toolDiv.style.display = 'none';
      } else {
        toolDiv.style.display = 'block';
        toolDiv.textContent = state.tool === 'Thinking...' ? 'Thinking...' : 'Running Tool: ' + state.tool;
      }
    }

    if (state.interrupt_disabled !== undefined) {
      document.getElementById('interrupt-btn').disabled = state.interrupt_disabled;
    }
  }
</script>

</body>
</html>
"""
