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

  #detailed-content {
    max-height: 140px;
    overflow-y: auto;
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
    <div onclick="sendAction('hide')" style="cursor: pointer; display: flex; align-items: center; justify-content: center; width: 24px; height: 24px; border-radius: 4px; transition: background 0.2s;" onmouseover="this.style.backgroundColor='rgba(127,127,127,0.2)'" onmouseout="this.style.backgroundColor='transparent'" title="Hide">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="5" y1="12" x2="19" y2="12"></line>
      </svg>
    </div>
  </div>

  <div class="content" id="content-area">
    <div onclick="toggleDetails()" style="display: flex; align-items: center; justify-content: space-between; cursor: pointer; margin-bottom: 4px;" title="Toggle Details">
      <div class="status" id="status-text" style="font-weight: bold; font-size: 13px;">Initializing...</div>
      <svg id="details-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="transition: transform 0.2s; color: var(--text-secondary);">
        <polyline points="6 9 12 15 18 9"></polyline>
      </svg>
    </div>
    <div id="detailed-content" style="display: none;">
      <div class="message" id="message-text"></div>
      <div class="tool-label" id="tool-text"></div>
    </div>
  </div>

  <div class="actions" id="actions-area">
    <button onclick="sendAction('show_chat')">Show Chat</button>
    <button id="interrupt-btn" onclick="sendAction('interrupt')">Interrupt</button>
  </div>
</div>

<script>
  let lastHeight = 0;
  let isMinimized = false;
  let isDetailedView = false;

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

  function handleMinimizeClick() {
      if (isMinimized) {
          sendAction('maximize');
      }
  }

  function toggleDetails() {
      isDetailedView = !isDetailedView;
      document.getElementById('detailed-content').style.display = isDetailedView ? 'block' : 'none';
      
      const chevron = document.getElementById('details-chevron');
      if (chevron) {
          chevron.style.transform = isDetailedView ? 'rotate(180deg)' : 'rotate(0deg)';
      }
      
      // Force immediate height recalculation
      setTimeout(() => {
          lastHeight = 0;
          const height = document.documentElement.scrollHeight;
          if (window.webkit && window.webkit.messageHandlers.overlay) {
              window.webkit.messageHandlers.overlay.postMessage({ type: 'resize', height: height });
          }
      }, 10);
  }

  document.getElementById('main-container').addEventListener('mousedown', handleMinimizeClick);

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
        let toolText = state.tool === 'Thinking...' ? 'Thinking...' : 'Running Tool: ' + state.tool;
        if (state.tool_input && Object.keys(state.tool_input).length > 0) {
           const inputStr = JSON.stringify(state.tool_input);
           const shortInput = inputStr.length > 100 ? inputStr.substring(0, 97) + '...' : inputStr;
           toolText += ' <span style="opacity: 0.6; font-size: 10px;">' + shortInput.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</span>';
        }
        toolDiv.innerHTML = toolText;
      }
    }

    if (state.status !== undefined) {
      const statusDiv = document.getElementById('status-text');
      statusDiv.textContent = state.status;
    }

    if (state.needs_input !== undefined) {
      const dot = document.getElementById('status-dot');
      if (state.needs_input) {
        dot.style.backgroundColor = '#ff9500'; // Orange
        dot.style.boxShadow = '0 0 8px #ff9500';
      } else {
        dot.style.backgroundColor = 'var(--accent)'; // Green
        dot.style.boxShadow = '0 0 8px var(--accent)';
      }
    }

    if (state.permission_request !== undefined) {
      const p = state.permission_request;
      let permArea = document.getElementById('permission-area');
      if (!permArea) {
        permArea = document.createElement('div');
        permArea.id = 'permission-area';
        permArea.style.marginTop = '8px';
        permArea.style.paddingTop = '8px';
        permArea.style.borderTop = '1px solid var(--border-color)';
        const detailedContent = document.getElementById('detailed-content');
        detailedContent.appendChild(permArea);
      }
      
      if (!p) {
        permArea.style.display = 'none';
      } else {
        permArea.style.display = 'block';
        const msgDiv = document.getElementById('message-text');
        if (msgDiv) msgDiv.style.display = 'none';
        const toolDiv = document.getElementById('tool-text');
        if (toolDiv) toolDiv.style.display = 'none';
        const toolName = p.tool_name || 'Tool';
        const reqId = p.request_id;
        const inputData = p.input || {};
        const cmd = inputData.command || inputData.cmd || '';
        const snippet = cmd ? cmd : JSON.stringify(inputData);
        // Clean up markdown/backticks in snippet if present
        const cleanSnippet = snippet.replace(/^```[a-z]*\\n/, '').replace(/\\n```$/, '').replace(/^`|`$/g, '');
        const snippetHtml = cleanSnippet ? `<pre style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 11px; max-height: 100px; overflow-y: auto; background: rgba(127, 127, 127, 0.2); padding: 6px; margin-top: 6px; border-radius: 4px; white-space: pre-wrap; word-break: break-all;">${cleanSnippet.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</pre>` : '';
        
        permArea.innerHTML = `
          <div style="font-weight: 600; font-size: 11px; margin-bottom: 4px;">${toolName} wants to run</div>
          ${snippetHtml}
          <div style="display: flex; gap: 6px; margin-top: 8px;">
            <button onclick="sendAction('permission_decision', {request_id: '${reqId}', decision: 'allow'})" style="background: var(--accent); color: white;">Allow once</button>
            <button onclick="sendAction('permission_decision', {request_id: '${reqId}', decision: 'allow_always'})" style="background: var(--accent); color: white;">Always</button>
            <button onclick="sendAction('permission_decision', {request_id: '${reqId}', decision: 'deny'})" style="background: rgba(255, 59, 48, 0.8); color: white;">Deny</button>
          </div>
        `;
        
        if (!isDetailedView) {
            toggleDetails();
        } else {
            setTimeout(() => {
                lastHeight = 0;
                const height = document.documentElement.scrollHeight;
                if (window.webkit && window.webkit.messageHandlers.overlay) {
                    window.webkit.messageHandlers.overlay.postMessage({ type: 'resize', height: height });
                }
            }, 10);
        }
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
