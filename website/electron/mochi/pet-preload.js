/**
 * pet-preload.js — the pet overlay's IPC bridge.
 *
 * Exposes `window.mochi`, which the ported renderer's `petBridge.ts` reads.
 * Only SHELL capabilities belong here: things that manipulate the Electron
 * window and cannot be done over HTTP. All of Mochi's DATA (watch list, pinned
 * files, chat) goes to the gateway over same-origin HTTP/WS instead — the pet
 * page is loaded FROM the gateway, so it already carries the auth cookie.
 *
 * Also sets `kirocrew.isElectron`, matching the dashboard's own preload. Two
 * reasons: `needsDesktopApp()` uses it for the App Store gate, and the panel's
 * BrowserPreviewNotice keys off it — without this the "browser preview" banner
 * would show inside the real desktop window.
 *
 * Security: contextIsolation is on and nodeIntegration off (see petWindow.js),
 * so this is the ONLY surface the pet page can reach. Keep it a fixed,
 * explicitly-enumerated list of channels — never expose `ipcRenderer` itself or
 * a pass-through `invoke(channel, ...)`, which would let page content reach any
 * handler in the main process.
 */

const { contextBridge, ipcRenderer } = require("electron");

/**
 * NEVER expose these — a deliberate, permanent exclusion list.
 *
 * The original standalone app's preload had all of them, and they are the reason
 * this list is written down rather than assumed. Each is either a credential
 * hand-off or a "point me at another backend" control:
 *
 *  - a getter that returned the gateway's own shared secret to page content.
 *    An app must never be able to read a credential it was not issued; exposing
 *    it here would defeat the app-token boundary completely, whatever the ACL on
 *    the routes says.
 *  - tunnel connect / disconnect and the backend-switch callbacks. A builtin is
 *    served BY the gateway, so there is no other backend to choose; "run the pet
 *    against a remote KiroCrew" is core's /api/instances feature instead (see
 *    panelBridge's remote-instances note), which never puts a secret in the page.
 *  - a generic `send(channel)` / `invoke(channel, ...)` relay. Enumerated
 *    channels only: a string-keyed relay is what let several menu actions rot
 *    with no handler and no error.
 *
 * Enforced by a unit test that asserts none of these names is exposed.
 */
const NEVER_EXPOSE = Object.freeze([
  // Credential hand-offs. Names are the neutral equivalents of the original's;
  // what matters is the CLASS of method, not the exact spelling.
  "getGatewaySecret",
  "getGatewayAuth",
  "startGateway",
  // "point me at a different backend" controls.
  "tunnelConnect",
  "tunnelDisconnect",
  "onBackendResolved",
  "onBackendSwitching",
  "onBackendSwitchError",
  // Generic channel relays.
  "send",
  "invoke",
]);

contextBridge.exposeInMainWorld("kirocrew", {
  platform: process.platform,
  isElectron: true,
});

/**
 * Expose the pet API, refusing at LOAD time to publish an excluded name.
 *
 * A comment alone would not stop the next port from re-adding one of these while
 * copying the original's preload; failing loudly here does, and the failure is at
 * startup rather than the first time page content calls it.
 */
function exposePetApi(api) {
  for (const name of NEVER_EXPOSE) {
    if (name in api) {
      throw new Error(`pet-preload must never expose "${name}" — see NEVER_EXPOSE`);
    }
  }
  contextBridge.exposeInMainWorld("mochi", api);
}

exposePetApi({
  /**
   * Report the pet's (and bubble's) hitbox in overlay-LOCAL coordinates.
   *
   * This is the load-bearing call: the main process polls the cursor against
   * these rectangles to decide whether the overlay accepts clicks. Passing null
   * puts the overlay back into full click-through.
   */
  /**
   * Cross-display drag. The renderer cannot follow the cursor past the overlay
   * edge (mousemove stops there), so it hands the drag to the main process,
   * which polls the global cursor and drives position + display handoff.
   * PORTED from the original main/preload.ts pet section; `pet:` -> `mochi-pet:`.
   */
  /** Move the pet to another monitor (tool-driven, no drag). */
  transferToDisplay: (displayId, x, y) =>
    ipcRenderer.send("mochi-pet:transfer-display", displayId, x, y),
  dragStart: (offsetX, offsetY) => ipcRenderer.send("mochi-pet:drag-start", offsetX, offsetY),
  dragEnd: () => ipcRenderer.send("mochi-pet:drag-end"),
  dragMouseup: () => ipcRenderer.send("mochi-pet:drag-mouseup"),
  onDragUpdate: (cb) => {
    const handler = (_e, x, y) => cb(x, y);
    ipcRenderer.on("mochi-pet:drag-update", handler);
    return () => ipcRenderer.removeListener("mochi-pet:drag-update", handler);
  },
  onDragEnded: (cb) => {
    const handler = (_e, x, y) => cb(x, y);
    ipcRenderer.on("mochi-pet:drag-ended", handler);
    return () => ipcRenderer.removeListener("mochi-pet:drag-ended", handler);
  },
  /** Every overlay listens for mouseup during a drag, so a release on ANY
   *  screen ends it — not just the one the drag started on. */
  onDragListenMouseup: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("mochi-pet:drag-listen-mouseup", handler);
    return () => ipcRenderer.removeListener("mochi-pet:drag-listen-mouseup", handler);
  },
  /** Legacy direct setter, kept because upstream kept it for edge cases. */
  setIgnoreMouse: (ignore) => ipcRenderer.send("mochi-pet:ignore-mouse", ignore),
  updateHitbox: (pet, bubble) =>
    ipcRenderer.send("mochi-pet:update-hitbox", pet, bubble),

  /**
   * Clicking the pet toggles the chat panel. The original bound this to
   * 'pet:toggle-expand'; the name is kept as `openChat` because that is what
   * the ported PetWidget calls.
   */
  openChat: () => ipcRenderer.send("mochi-pet:open-chat"),

  /**
   * Signals that the avatar picker's choice has been SAVED (not merely made).
   * The shell closes the picker on this, so it must fire after the settings
   * write succeeds — firing it early would close the window on a failed save
   * and strand the user with no avatar and no prompt.
   */
  avatarChosen: () => ipcRenderer.send("mochi-avatar:chosen"),

  /**
   * Open the Avatars window (the original called this surface the Gallery).
   * Reached from the pet's right-click menu and the chat panel's menu, so the
   * choice made on first run stays reversible.
   */
  openAvatars: () => ipcRenderer.send("mochi-avatar:open"),

  /**
   * Open the chat panel focused on a specific view.
   *
   * The pet overlay is a SEPARATE window from the panel, so a pet-menu item
   * that shows panel content (Memories, Settings) cannot flip the panel's
   * renderer state directly — it asks the main process to open/focus the panel
   * and tell it which view to show (see panelWindow.showPanelView). These are
   * dedicated per-view channels, NOT one generic `showView(name)` relay: an
   * enumerated surface is the rule this preload's header states for every
   * channel, and it is what a generic renderer→main string relay violated (that
   * is exactly the bug that let the old menu actions rot — no handler, no error).
   */
  openMemories: () => ipcRenderer.send("mochi-pet:open-memories"),
  openSettings: () => ipcRenderer.send("mochi-pet:open-settings"),
  // The settings WINDOW closing itself (there is no in-panel overlay to
  // dismiss any more).
  closeSettings: () => ipcRenderer.send("mochi-settings:close"),
  /**
   * Hide/show every Mochi window — the same toggle the hideAll accelerator
   * drives. The pet's context menu "Hide" means THIS, not disabling the app:
   * disabling tears the app down through the app manager and takes seconds.
   */
  hideAll: () => ipcRenderer.send("mochi-pet:hide-all"),
  /**
   * Apply rebound global accelerators NOW and report what the OS accepted:
   * `{toggleWindow?: boolean, hideAll?: boolean}`. `false` means another app owns
   * that combination. Named per-purpose rather than a generic invoke relay (see
   * NEVER_EXPOSE); the settings save itself still goes over HTTP.
   */
  applyShortcuts: (accelerators) => ipcRenderer.invoke("mochi-shortcuts:apply", accelerators),
  // Open a local image in the OS viewer. Enumerated, not a generic relay: the
  // main process re-validates the path (image extension, realpath, regular file).
  openImage: (filePath) => ipcRenderer.invoke("mochi-pet:open-image", filePath),

  /**
   * Open KiroCrew's dashboard in the user's default browser.
   *
   * The dashboard IS the gateway origin, so the panel's own window-open handler
   * would load it INSIDE the panel; a dedicated channel routes it through
   * shell.openExternal in the main process instead. Deliberately takes NO url
   * argument — the main process supplies its own gateway origin, so page
   * content can never ask the shell to open an arbitrary external URL.
   */
  openDashboard: () => ipcRenderer.send("mochi-panel:open-dashboard"),
  // Pet-menu rows whose implementation lives in the panel (shared menu).
  clearScreenInPanel: () => ipcRenderer.send("mochi-pet:clear-screen"),
  deleteHistoryInPanel: () => ipcRenderer.send("mochi-pet:delete-history"),

  /**
   * Hide the chat panel (the title-bar close button).
   *
   * The main process has always had this handler — the panel is a hidden
   * singleton, so closing means hide — but nothing exposed a sender, which is
   * why the button silently did nothing.
   */
  closeChat: () => ipcRenderer.send("mochi-panel:close"),

  /**
   * Reveal a file in Finder / the OS file manager, and open an http(s) link in
   * the default browser.
   *
   * Both take an argument from page content, so BOTH are validated in the main
   * process — a path must be a real existing file, and a URL must be http(s).
   * Without that, "the renderer can ask the shell to open anything" is a real
   * escape from the panel's sandbox, not a theoretical one.
   */
  revealFile: (path) => ipcRenderer.send("mochi-panel:reveal-file", path),
  openExternal: (url) => ipcRenderer.send("mochi-panel:open-external", url),

  /**
   * Set the chat panel's WIDTH only (px). The main process clamps the value and
   * preserves the panel's height and position. Cross-agent contract channel —
   * the caller is coded against this exact name.
   */
  setPanelWidth: (w) => ipcRenderer.send("mochi-panel:set-width", w),

  /**
   * Panel-side receivers for the pet-initiated view switches above. The panel's
   * ChatPanel subscribes on mount; the main process sends once the panel page
   * has loaded (panelWindow.showPanelView handles the first-open timing). Each
   * returns an unsubscribe so the ported cleanup (`off?.()`) works.
   */
  onOpenMemories: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("mochi-panel:show-memories", handler);
    return () => ipcRenderer.removeListener("mochi-panel:show-memories", handler);
  },
  onClearScreen: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("mochi-panel:clear-screen", handler);
    return () => ipcRenderer.removeListener("mochi-panel:clear-screen", handler);
  },
  /**
   * The global screen-capture accelerator fired.
   *
   * KEPT for the in-panel fallback path. In the Electron shell the accelerator
   * now opens the dedicated crop window (snipWindow.js) instead, because the crop
   * surface has to be the size of the screen — hosted in this 320px panel the
   * captured frame scaled down so far that a pixel of drag moved ~13 source
   * pixels. This channel still fires for surfaces that have no crop window.
   */
  onStartSnip: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("mochi-panel:start-snip", handler);
    return () => ipcRenderer.removeListener("mochi-panel:start-snip", handler);
  },

  // ── Crop window ───────────────────────────────────────────────────────────
  //
  // Three one-way messages from the crop window, plus one delivery INTO the
  // panel. Split this way because the crop window and the panel are different
  // renderers: the crop cannot reach the panel's composer directly, and only the
  // CROPPED png crosses the boundary — never the full frame, which is several
  // megabytes at 4K.

  /** Crop window: a frame is in hand, it is safe to cover the screen now. */
  snipReady: () => ipcRenderer.send("mochi-snip:ready"),
  /** Crop window: the user selected a region; hand the PNG (bare base64) over. */
  snipResult: (base64) => ipcRenderer.send("mochi-snip:result", base64),
  /** Crop window: done, cancelled, or refused — tear the surface down. */
  snipClose: () => ipcRenderer.send("mochi-snip:close"),
  /** Panel: a crop arrived from the crop window; attach it to the composer. */
  onSnipDelivered: (cb) => {
    const handler = (_e, base64) => cb(base64);
    ipcRenderer.on("mochi-panel:snip-delivered", handler);
    return () => ipcRenderer.removeListener("mochi-panel:snip-delivered", handler);
  },

  onDeleteHistory: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("mochi-panel:delete-history", handler);
    return () => ipcRenderer.removeListener("mochi-panel:delete-history", handler);
  },

  /**
   * Report the open context menu's rectangle (overlay-LOCAL coords), or null
   * once it closes.
   *
   * Separate from `updateHitbox` on purpose: the menu is owned by a different
   * component with a different lifetime, and the original kept it on its own
   * channel too. Without it the menu is drawn inside the click-through overlay
   * with no hitbox, so every click on a row lands on whatever is behind the pet.
   */
  setMenuHitbox: (rect) => ipcRenderer.send("mochi-pet:menu-hitbox", rect),

  /**
   * Menu open/close notifications. While open, the overlay accepts ALL clicks so
   * that clicking outside the menu dismisses it; on close the cursor poll takes
   * click-through back over.
   *
   * Named methods rather than the original's `send(channel)` allowlist relay —
   * an enumerated surface is this preload's rule, and a string-keyed relay is
   * exactly what rotted before (no handler, no error).
   */
  menuOpened: () => ipcRenderer.send("mochi-pet:menu-open"),
  menuClosed: () => ipcRenderer.send("mochi-pet:menu-close"),

  getWindowPosition: () => ipcRenderer.invoke("mochi-pet:get-position"),
  savePosition: (x, y) => ipcRenderer.send("mochi-pet:save-position", x, y),

  /**
   * Activation handshake — main -> renderer.
   *
   * REQUIRED, not optional: useDisplayActivation starts with isActive=false and
   * only flips on this event, and PetWidget returns null while inactive. Without
   * it the overlay renders nothing at all, which on a transparent window looks
   * exactly like "the pet never appeared".
   *
   * Payload mirrors the original: (active, x?, y?, isDragging?).
   */
  onSetActive: (cb) => {
    const handler = (_e, active, x, y, isDragging) => cb(active, x, y, isDragging);
    ipcRenderer.on("mochi-pet:set-active", handler);
    return () => ipcRenderer.removeListener("mochi-pet:set-active", handler);
  },

  /** Display geometry for edge detection / cross-display walking.
   *
   * `activeDisplayId` is the display the pet is ACTUALLY on, which is not the
   * same as `myDisplayId` — this event goes to every overlay, and each one used
   * to report its own id as the active one, so whichever POST landed last
   * decided what the pet believed. The shell is the only party that knows, so it
   * says so explicitly.
   */
  onDisplaysInfo: (cb) => {
    const handler = (_e, displays, myDisplayId, activeDisplayId) =>
      cb(displays, myDisplayId, activeDisplayId);
    ipcRenderer.on("mochi-pet:displays-info", handler);
    return () => ipcRenderer.removeListener("mochi-pet:displays-info", handler);
  },

  /**
   * Per-instance "is Mochi turned on there", keyed by instance id.
   *
   * The one fact the instance switcher cannot fetch for itself: Settings is
   * served by the LOCAL gateway, and asking a remote needs that remote's token,
   * which only the shell holds (core mints it via the local control plane). The
   * rest of the switcher reads core's /api/instances directly over HTTP.
   */
  instancesEnabledMap: () => ipcRenderer.invoke("mochi-instances:enabled-map"),

  /**
   * The per-MACHINE prefs — `{petInstance, shortcuts}` — from the SHELL's store.
   *
   * NOT read over HTTP, and that is the whole point. Every Mochi window is loaded
   * FROM the gateway it shows, and this seam's HTTP calls are same-origin, so a
   * pet showing a REMOTE would read and write that remote's copy of a choice that
   * belongs to this computer. That mismatch made the instance switch a one-way
   * door: the write landed where nothing reads it, and no surface was left that
   * could move the pet back.
   */
  machinePrefs: () => ipcRenderer.invoke("mochi-machine:get"),

  /**
   * Point the pet at an instance AND move it, in one call.
   *
   * One handler rather than "POST the setting, then ask the shell to apply it":
   * those were two steps against two different gateways, which is how a saved
   * choice ended up with nothing acting on it.
   */
  setPetInstance: (instanceId) => ipcRenderer.invoke("mochi-instances:set", instanceId),

  /**
   * Core's instance list for THIS MACHINE's host gateway.
   *
   * The switcher's own `fetch('/api/instances')` is same-origin, so on a remote
   * pet it listed the REMOTE's registry — possibly empty, possibly a different
   * set of crews, and missing the one the user wanted to go back to. The host owns
   * the registry the stored ids refer to.
   */
  instancesList: () => ipcRenderer.invoke("mochi-instances:list"),

  /**
   * Apply a just-saved `petInstance` immediately rather than on the shell's next
   * reconcile pass (up to 5s later, which reads as "the switch didn't work").
   * Resolves once the windows have actually been rebuilt, so the caller can keep
   * its row busy until then.
   */
  applyInstanceNow: () => ipcRenderer.invoke("mochi-instances:apply-now"),
});
