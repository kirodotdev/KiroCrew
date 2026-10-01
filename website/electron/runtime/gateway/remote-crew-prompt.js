"use strict";

const {
  TUNNEL_OPTION_LABEL,
  parseRemoteCrewFields,
  remoteCrewDraft,
  tunnelOptionHint,
} = require("../../remote-crew-setup");
const { DEFAULT_REMOTE_BIN, DEFAULT_REMOTE_PATH } = require("../../remote-token");

/**
 * The failure dialog's "Add / Edit Remote Crew" form: a small modal window
 * that collects a crew's host, binary path, remote port and remote PATH for
 * one local port. Saving and validation stay with the caller
 * (saveRemoteCrewConfig), so a refused save can reopen the form on what the
 * user typed.
 */
function createRemoteCrewPrompt({ BrowserWindow, nativeTheme, isWindows = false }) {
  /**
   * Collect a remote crew's address for `promptPort`, opening on `initial`.
   * Resolves with what the user saved, or null when the window was closed
   * without saving.
   */
  function promptRemoteCrew(parentWindow, promptPort, initial = {}) {
    return new Promise((resolve) => {
      const dark = nativeTheme.shouldUseDarkColors;
      const hasParent = parentWindow && !parentWindow.isDestroyed();
      const opening = remoteCrewDraft(initial);
      const promptWindow = new BrowserWindow({
        width: 480,
        // Four labelled fields and (off Windows) the tunnel option, each with a hint.
        height: isWindows ? 470 : 540,
        resizable: false,
        useContentSize: true,
        parent: hasParent ? parentWindow : undefined,
        modal: !!hasParent,
        backgroundColor: dark ? "#1e293b" : "#f8fafc",
        webPreferences: { nodeIntegration: false, contextIsolation: true },
      });
      promptWindow.setMenu(null);

      const escapeAttr = (value) => String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
      const foreground = dark ? "#e2e8f0" : "#1e293b";
      const muted = dark ? "#94a3b8" : "#64748b";
      const html = `<!DOCTYPE html><html><head><style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,sans-serif; padding:20px; background:${dark ? "#1e293b" : "#f8fafc"}; color:${foreground}; }
        .title { font-size:15px; font-weight:700; margin-bottom:10px; }
        label { display:block; font-size:12px; font-weight:600; margin:10px 0 4px; }
        .hint { font-size:11px; color:${muted}; margin-top:4px; }
        label.check { display:flex; align-items:center; gap:6px; margin-top:14px; }
        label.check input { width:auto; }
        input { width:100%; padding:7px 8px; border-radius:6px; font-size:13px;
          border:1px solid ${dark ? "#475569" : "#cbd5e1"};
          background:${dark ? "#0f172a" : "#ffffff"}; color:${foreground}; }
        .row { display:flex; gap:8px; margin-top:18px; }
        button { flex:1; padding:9px; border-radius:6px; border:none; cursor:pointer; font-size:13px; font-weight:600; }
        .ok { background:#f97316; color:#fff; } .ok:hover { background:#ea580c; }
        .cancel { background:${dark ? "#334155" : "#e2e8f0"}; color:${dark ? "#94a3b8" : "#475569"}; }
        .cancel:hover { background:${dark ? "#475569" : "#cbd5e1"}; }
      </style></head><body>
        <div class="title">Remote crew for port ${escapeAttr(promptPort)}</div>
        <label>Host</label>
        <input id="h" value="${escapeAttr(opening.host)}" placeholder="myhost.example.com" autofocus>
        <div class="hint">An SSH host or a name from your SSH config.</div>
        <label>kirocrew binary path</label>
        <input id="b" value="${escapeAttr(opening.binPath)}" placeholder="${escapeAttr(DEFAULT_REMOTE_BIN)}">
        <div class="hint">Leave blank for ${escapeAttr(DEFAULT_REMOTE_BIN)}.</div>
        <label>Remote port</label>
        <input id="rp" value="${escapeAttr(opening.remotePort)}" placeholder="${escapeAttr(promptPort)}">
        <div class="hint">The port the crew serves on its own machine. Leave blank if it is also ${escapeAttr(promptPort)}.</div>
        <label>Remote PATH</label>
        <input id="pa" value="${escapeAttr(opening.remotePath)}" placeholder="${escapeAttr(DEFAULT_REMOTE_PATH)}">
        <div class="hint">Leave blank for ${escapeAttr(DEFAULT_REMOTE_PATH)}.</div>
        ${isWindows ? "" : `<label class="check"><input type="checkbox" id="mt"${opening.manageTunnel ? " checked" : ""}> ${TUNNEL_OPTION_LABEL}</label>
        <div class="hint">${escapeAttr(tunnelOptionHint(promptPort))}</div>`}
        <div class="row">
          <button class="ok" onclick="save()">Save &amp; Retry</button>
          <button class="cancel" onclick="window.close()">Cancel</button>
        </div>
        <script>
          function save() {
            document.title = JSON.stringify({
              host: document.getElementById('h').value.trim(),
              binPath: document.getElementById('b').value.trim(),
              remotePort: document.getElementById('rp').value.trim(),
              remotePath: document.getElementById('pa').value.trim(),
              manageTunnel: !!(document.getElementById('mt') || {}).checked,
            });
            window.close();
          }
          document.addEventListener('keydown', event => {
            if (event.key === 'Enter') save();
            if (event.key === 'Escape') window.close();
          });
        </script>
      </body></html>`;

      let savedTitle = null;
      promptWindow.on("page-title-updated", (_event, updatedTitle) => {
        savedTitle = updatedTitle;
      });
      promptWindow.on("closed", () => resolve(parseRemoteCrewFields(savedTitle)));
      promptWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
    });
  }

  return { promptRemoteCrew };
}

module.exports = { createRemoteCrewPrompt };
