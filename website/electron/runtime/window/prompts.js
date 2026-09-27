"use strict";

/**
 * The small modal forms the window menus open over a dashboard window: the
 * gateway-port prompt behind New Connection Window, and Rename Window. Each
 * form is a data: URL page that reports its answer through its title, so the
 * page needs no preload and no IPC.
 *
 * Forms take the focused dashboard's own theme variables when its document can
 * report them, and the native dark/light palette otherwise.
 */
function createWindowPrompts({
  BaseWindow,
  BrowserWindow,
  nativeTheme,
  store,
  getMainWindow,
}) {
  async function getDashboardThemeVars() {
    const win = BaseWindow.getFocusedWindow() || getMainWindow();
    if (!win || win.isDestroyed()) return null;
    try {
      return await win.webContents.executeJavaScript(`
        (() => {
          const s = getComputedStyle(document.documentElement);
          return {
            bg: s.getPropertyValue('--bg').trim(),
            card: s.getPropertyValue('--card').trim(),
            text: s.getPropertyValue('--text').trim(),
            muted: s.getPropertyValue('--muted').trim(),
            border: s.getPropertyValue('--border').trim(),
            accent: s.getPropertyValue('--accent').trim(),
            accentHover: s.getPropertyValue('--accent-hover').trim(),
            bgAccent: s.getPropertyValue('--bg-accent').trim(),
          };
        })()
      `);
    } catch {
      return null;
    }
  }

  function modalCSSForMode(dark) {
    return `* { margin:0; padding:0; box-sizing:border-box; }
      body { font-family:-apple-system,sans-serif; padding:24px; background:${dark ? "#1e293b" : "#f8fafc"}; color:${dark ? "#e2e8f0" : "#1e293b"}; }
      label { display:block; margin-bottom:8px; font-size:13px; color:${dark ? "#94a3b8" : "#64748b"}; }
      input { width:100%; padding:10px; border-radius:6px; border:1px solid ${dark ? "#475569" : "#cbd5e1"};
        background:${dark ? "#0f172a" : "#ffffff"}; color:${dark ? "#e2e8f0" : "#1e293b"}; font-size:14px; outline:none; margin-bottom:12px; }
      input:focus { border-color:#f97316; }
      .hint { font-size:11px; color:${dark ? "#64748b" : "#94a3b8"}; margin-bottom:12px; }
      .row { display:flex; gap:8px; }
      button { flex:1; padding:8px; border-radius:6px; border:none; cursor:pointer; font-size:13px; font-weight:600; }
      .ok { background:#f97316; color:#fff; } .ok:hover { background:#ea580c; }
      .cancel { background:${dark ? "#334155" : "#e2e8f0"}; color:${dark ? "#94a3b8" : "#475569"}; } .cancel:hover { background:${dark ? "#475569" : "#cbd5e1"}; }`;
  }

  function modalCSSFromVars(v) {
    return `* { margin:0; padding:0; box-sizing:border-box; }
      body { font-family:-apple-system,sans-serif; padding:24px; background:${v.bg}; color:${v.text}; }
      label { display:block; margin-bottom:8px; font-size:13px; color:${v.muted}; }
      input { width:100%; padding:10px; border-radius:6px; border:1px solid ${v.border};
        background:${v.card}; color:${v.text}; font-size:14px; outline:none; margin-bottom:12px; }
      input:focus { border-color:${v.accent}; }
      .hint { font-size:11px; color:${v.muted}; margin-bottom:12px; }
      .row { display:flex; gap:8px; }
      button { flex:1; padding:8px; border-radius:6px; border:none; cursor:pointer; font-size:13px; font-weight:600; }
      .ok { background:${v.accent}; color:#fff; } .ok:hover { background:${v.accentHover || v.accent}; }
      .cancel { background:${v.bgAccent || v.card}; color:${v.muted}; } .cancel:hover { background:${v.border}; }`;
  }

  async function getModalCSS() {
    const vars = await getDashboardThemeVars();
    if (vars && vars.bg) return modalCSSFromVars(vars);
    return modalCSSForMode(nativeTheme.shouldUseDarkColors);
  }

  /**
   * Ask for another local gateway port over the window `getParent()` names
   * once the form's styling is ready. `onPort` receives a port in 1..65535; a
   * cancel or an out-of-range answer calls nothing.
   */
  async function promptConnectionPort(getParent, onPort) {
    const css = await getModalCSS();
    const promptWin = new BrowserWindow({
      width: 400,
      height: 180,
      resizable: false,
      useContentSize: true,
      parent: getParent(),
      modal: true,
      backgroundColor: "#00000000",
      webPreferences: { nodeIntegration: false, contextIsolation: true },
    });
    const html = `<!DOCTYPE html><html><head><style>
      ${css}
    </style></head><body>
      <label>Gateway port</label>
      <input id="p" type="number" value="7778" min="1" max="65535" autofocus>
      <div class="hint">Connect to a Kiro Crew gateway running on another port</div>
      <div class="row"><button class="ok" onclick="go()">Connect</button>
      <button class="cancel" onclick="window.close()">Cancel</button></div>
      <script>
        function go() {
          document.title = document.getElementById('p').value.trim();
          window.close();
        }
        document.addEventListener('keydown', event => {
          if (event.key === 'Enter') go();
          if (event.key === 'Escape') window.close();
        });
      </script>
    </body></html>`;
    promptWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
    promptWin.setMenu(null);

    let savedTitle = null;
    promptWin.on("page-title-updated", (_event, title) => {
      savedTitle = title;
    });
    promptWin.on("closed", async () => {
      if (!savedTitle) return;
      const connectionPort = parseInt(savedTitle, 10);
      if (
        Number.isNaN(connectionPort)
        || connectionPort < 1
        || connectionPort > 65535
      ) return;
      await onPort(connectionPort);
    });
  }

  function renameFocusedWindow() {
    const focused = BaseWindow.getFocusedWindow();
    if (!focused || !focused._mcSetCustomName) return;

    const currentTitle = focused.getTitle();
    const focusedPort = focused._mcBackendUrl
      ? new URL(focused._mcBackendUrl).port
      : "";
    const esc = (value) => value
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    getDashboardThemeVars().then((vars) => {
      const css = vars && vars.bg
        ? modalCSSFromVars(vars)
        : modalCSSForMode(nativeTheme.shouldUseDarkColors);
      const promptWin = new BrowserWindow({
        width: 400,
        height: 200,
        resizable: false,
        useContentSize: true,
        parent: focused,
        modal: true,
        backgroundColor: "#00000000",
        webPreferences: { nodeIntegration: false, contextIsolation: true },
      });
      const html = `<!DOCTYPE html><html><head><style>
        ${css}
        .check-row { display:flex; align-items:center; gap:6px; margin-top:8px; }
        .check-row input { width:auto; margin:0; }
        .check-row label { margin:0; font-size:12px; }
      </style></head><body>
        <label>Window name</label>
        <input id="n" value="${esc(currentTitle.replace(/^Kiro ?Crew /g, ""))}" autofocus>
        <div class="row"><button class="ok" onclick="go()">Rename</button>
        <button class="cancel" onclick="window.close()">Cancel</button></div>
        <div class="check-row"><input type="checkbox" id="d"><label for="d">Set as default name for :${focusedPort} windows</label></div>
        <script>
          function go() {
            document.title = JSON.stringify({
              name: document.getElementById('n').value.trim(),
              setDefault: document.getElementById('d').checked,
            });
            window.close();
          }
          document.addEventListener('keydown', event => {
            if (event.key === 'Enter') go();
            if (event.key === 'Escape') window.close();
          });
        </script>
      </body></html>`;
      promptWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
      promptWin.setMenu(null);

      let savedTitle = null;
      promptWin.on("page-title-updated", (_event, title) => {
        savedTitle = title;
      });
      promptWin.on("closed", () => {
        if (!savedTitle || !focused || focused.isDestroyed()) return;
        try {
          const { name, setDefault } = JSON.parse(savedTitle);
          if (name) {
            focused._mcSetCustomName(name);
            if (setDefault && focusedPort) {
              const hosts = store.get("remoteHosts") || {};
              const key = String(focusedPort);
              hosts[key] = { ...(hosts[key] || {}), defaultName: name };
              store.set("remoteHosts", hosts);
            }
          }
        } catch {
          // Legacy plain-text fallback.
          if (savedTitle) focused._mcSetCustomName(savedTitle);
        }
      });
    });
  }

  return { getModalCSS, promptConnectionPort, renameFocusedWindow };
}

module.exports = { createWindowPrompts };
