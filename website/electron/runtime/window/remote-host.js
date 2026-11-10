"use strict";

async function promptRemoteHost(deps) {
    const focused = deps.BaseWindow.getFocusedWindow() || deps.mainWindow;
    if (!focused || focused.isDestroyed() || !focused._mcBackendUrl) return;
    // Keyed with `defaultedPort` so the entry this form WRITES lands under the
    // same key `isGatewayLocalForWindow` READS. A raw `URL.port` is "" on a
    // scheme-default port, so the two would disagree on :80 and a crew the user
    // configured here would classify as a gateway on this machine.
    const focusedPort = (0, deps.defaultedPort)(focused._mcBackendUrl);
    // Read through the resolver so a crew an older version left under the empty
    // key pre-fills this form: stating it again is what moves the record onto
    // the resolved key, and the write below retires the old one.
    const config = (0, deps.getRemoteHostConfigForUrl)(deps.store, focused._mcBackendUrl);
    const currentHost = config?.host || "";
    const currentBin = config?.binPath || deps.DEFAULT_REMOTE_BIN;
    const currentRemotePort = config?.remotePort || "";
    const currentRemotePath = config?.remotePath || "";
    // The app keeps a tunnel only for the port it launched on, so this form
    // offers the option there alone; for any other tab it carries the stored
    // choice over unchanged.
    const isLaunchPort = String(focusedPort) === String(deps.port);
    const offerTunnel = isLaunchPort && !deps.IS_WINDOWS;
    const currentManageTunnel = (0, deps.remoteCrewDraft)(config || {}, { tunnelByDefault: offerTunnel }).manageTunnel;

    const css = await (0, deps.getModalCSS)();
    const esc = (value) => value
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    const promptWin = new deps.BrowserWindow({
      width: 480,
      height: offerTunnel ? 520 : 400,
      resizable: false,
      useContentSize: true,
      parent: focused,
      modal: true,
      backgroundColor: "#00000000",
      webPreferences: { nodeIntegration: false, contextIsolation: true },
    });
    const html = `<!DOCTYPE html><html><head><style>
      ${css}
    </style></head><body>
      <label>Remote host for :${focusedPort}</label>
      <input id="h" value="${esc(currentHost)}" placeholder="myhost.corp.example.com" autofocus>
      <div class="hint">Leave empty to use local token (no SSH).</div>
      <label>kirocrew binary path</label>
      <input id="b" value="${esc(currentBin)}" placeholder="${deps.DEFAULT_REMOTE_BIN}">
      <label>Remote port <span style="font-weight:normal;opacity:0.6">(default: same as tab = ${focusedPort})</span></label>
      <input id="rp" value="${esc(currentRemotePort)}" placeholder="${focusedPort}">
      <label>Remote PATH <span style="font-weight:normal;opacity:0.6">(default: ${deps.DEFAULT_REMOTE_PATH})</span></label>
      <input id="pa" value="${esc(currentRemotePath)}" placeholder="${deps.DEFAULT_REMOTE_PATH}">
      ${offerTunnel ? `<label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="mt" style="width:auto"${currentManageTunnel ? " checked" : ""}> ${deps.TUNNEL_OPTION_LABEL}</label>
      <div class="hint">${esc((0, deps.tunnelOptionHint)(focusedPort))}</div>` : ""}
      <div class="row"><button class="ok" onclick="save()">Save</button>
      <button class="cancel" onclick="window.close()">Cancel</button></div>
      <script>
        function save() {
          document.title = JSON.stringify({
            host: document.getElementById('h').value.trim(),
            binPath: document.getElementById('b').value.trim(),
            remotePort: document.getElementById('rp').value.trim(),
            remotePath: document.getElementById('pa').value.trim(),
            // Read from the checkbox where this form offers it; elsewhere the
            // stored choice rides through unchanged, never silently dropped.
            manageTunnel: ${offerTunnel ? "document.getElementById('mt').checked" : currentManageTunnel ? "true" : "false"},
          });
          window.close();
        }
        document.addEventListener('keydown', event => {
          // Enter on a focused button is that button's own press: Cancel must not save.
          if (event.key === 'Enter' && !(event.target instanceof HTMLButtonElement)) save();
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
      try {
        const fields = (0, deps.parseRemoteCrewFields)(savedTitle);
        if (fields) {
          const { host } = fields;
          const parent = focused && !focused.isDestroyed() ? focused : null;
          // A record an older version left under the empty key is superseded by
          // what the user states here, but only ONCE that statement is durable.
          // Retiring first would erase the record on a write that is refused --
          // and `saveRemoteCrewConfig` refuses an unselectable port outright, so
          // on an http window whose port resolves to 80 no write can ever
          // succeed. The crew would then read as a gateway on this machine, with
          // no way back, and X-Internal-Secret would go through the tunnel.
          //
          // Only a URL whose port is its scheme's default could have produced
          // that record, so a window on any other port leaves it alone.
          const retireLegacy = () => {
            if ((0, deps.portIsSchemeDefault)(focused._mcBackendUrl)) {
              (0, deps.retireLegacyEmptyPortHost)(deps.store, focusedPort);
            }
          };
          if (!host) {
            // A scheme-default port (:80/:443) can't hold a per-port record the
            // user can rewrite (saveRemoteCrewConfig refuses to WRITE such a
            // port), so a crew reached on one is marked remote either by the
            // empty-key legacy record OR by a legacy record left under the
            // resolved key "80"/"443" itself. Deleting the resolved-key record
            // AND retiring the legacy one here would leave a FRESH default-port
            // window later reading no crew at all, so isGatewayLocalForWindow
            // classifies the loopback window as a local gateway and the
            // host-presence heartbeat sends X-Internal-Secret through the
            // still-open ssh tunnel. A tunnelled crew must keep its record.
            //
            // There is no in-app probe that a scheme-default port is positively
            // local (a :80 write is refused by saveRemoteCrewConfig), so the only
            // fail-closed answer is to REFUSE the Clear and keep the record
            // intact -- a future default-port window still resolves REMOTE from
            // the store itself, not merely from a window-scoped latch. Resolve
            // through getRemoteHostConfigForUrl (not the empty key alone) so a
            // host under the resolved "80"/"443" key is also caught. Direct the
            // user to reopen the crew on a selectable port and clear it there,
            // where a per-port record can be written and removed.
            if (
              (0, deps.portIsSchemeDefault)(focused._mcBackendUrl)
              && (0, deps.getRemoteHostConfigForUrl)(deps.store, focused._mcBackendUrl)?.host
            ) {
              deps.dialog.showMessageBox(parent, {
                type: "error",
                title: "Can't clear remote host from this tab",
                message:
                  `This crew was reached on a default port (:${focusedPort}), so the app can't change or clear its remote-host setting from this tab. The setting is kept on purpose so this window is never mistaken for a local gateway while the connection is open. To stop reaching this crew, close this tab (and stop the "ssh -L :${focusedPort}" tunnel if you started one). A crew opened on a selectable port (for example "ssh -L 7777:…") can be cleared from its own tab.`,
              });
              return;
            }
            // Clearing belongs to this surface: the shared writer stores a crew
            // and refuses an empty host. A scheme-default port with a host-bearing
            // legacy record was already intercepted by the refusal above, and on
            // any other port there is no empty-key record to retire, so no
            // retirement runs here.
            (0, deps.setRemoteHostConfig)(deps.store, focusedPort, {});
            if (isLaunchPort) (0, deps.syncTunnel)();
            const cleared = `Remote host for :${focusedPort} cleared (using local token)`;
            console.log(cleared);
            deps.dialog.showMessageBox(parent, { message: cleared, type: "info" });
            return;
          }
          const { saved, error } = (0, deps.saveRemoteCrewConfig)(deps.store, focusedPort, fields);
          if (!saved) {
            deps.dialog.showMessageBox(parent, {
              type: "error",
              title: "Invalid Input",
              message: error,
            });
            return;
          }
          if (isLaunchPort) (0, deps.syncTunnel)();
          retireLegacy();
          const message = `Remote host for :${focusedPort} set to ${host}`;
          console.log(message);
          deps.dialog.showMessageBox(parent, { message, type: "info" });
        }
      } catch (error) {
        console.error("Failed to parse remote host settings:", error.message);
      }
    });
  }

module.exports = { promptRemoteHost };
