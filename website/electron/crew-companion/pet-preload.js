/**
 * pet-preload.js — the overlay's only bridge to the main process.
 *
 * Deliberately tiny. The renderer talks to the BACKEND over same-origin HTTP (it is
 * a page served by the gateway, so its cookie is present), which leaves exactly one
 * thing it cannot do for itself: tell the window whether the pointer is currently
 * over the companion, so input can be accepted there and refused everywhere else.
 *
 * `contextIsolation` is on and `nodeIntegration` off, so this is the whole surface —
 * no `require`, no filesystem, no arbitrary IPC.
 */

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("crewCompanion", {
  /**
   * Report the companion's and bubble's hitboxes for this window.
   *
   * The window covers the whole display, so leaving input enabled would make the
   * desktop unclickable. Rather than toggle input as the pointer enters and leaves
   * the sprite — which needed an IPC round-trip and let a fast click fall through —
   * the renderer hands the main process the rects and the main process polls the
   * cursor and toggles ignore-mouse itself.
   *
   * @param {{x:number,y:number,w:number,h:number}|null} pet
   * @param {{x:number,y:number,w:number,h:number}|null} bubble
   */
  updateHitbox(pet, bubble) {
    ipcRenderer.send("crew-companion:update-hitbox", pet || null, bubble || null);
  },

  /**
   * Report the context menu's rect while it is open, or null when it closes.
   *
   * Added as one more interactive hitbox so the menu is clickable while the rest of
   * the desktop stays click-through — no more making the whole window interactive
   * for the menu's lifetime.
   *
   * @param {{x:number,y:number,w:number,h:number}|null} rect
   */
  setMenuHitbox(rect) {
    ipcRenderer.send("crew-companion:menu-hitbox", rect || null);
  },

  /**
   * Grant or withdraw keyboard focus for this window.
   *
   * The overlay is created non-focusable on purpose: it covers the whole display and
   * must never steal focus from whatever the user is actually doing. But the panel
   * has a text input, and a non-focusable window cannot receive typing at all.
   *
   * So focus is granted only while the panel is open and withdrawn as soon as it
   * closes — the narrowest window in which the trade-off is worth making. The v1.0
   * spec flags this exact tension as the thing to verify before building the panel.
   *
   * @param {boolean} focusable true while the panel is open
   */
  setFocusable(focusable) {
    ipcRenderer.send("crew-companion:focusable", Boolean(focusable));
  },

  /**
   * Open the panel beside the companion.
   *
   * The companion's rect is passed in SCREEN coordinates because only the renderer
   * knows where inside its full-display overlay the companion currently sits — the
   * main process would have to guess, and would guess wrong the moment it is dragged.
   *
   * @param {{x:number,y:number,width:number,height:number}} petRect
   */
  panelOpen(petRect) {
    ipcRenderer.send("crew-companion:panel-open", petRect);
  },

  panelClose() {
    ipcRenderer.send("crew-companion:panel-close");
  },

  /**
   * Report that the breathing exercise is running, so a click elsewhere does not
   * close the panel and discard it.
   */
  panelBreathing(active) {
    ipcRenderer.send("crew-companion:panel-breathing", Boolean(active));
  },

  /** Report that a destination view is open, for the same reason. */
  panelHold(hold) {
    ipcRenderer.send("crew-companion:panel-hold", Boolean(hold));
  },

  /**
   * The panel window has closed.
   *
   * The companion needs this because the panel can be dismissed without it: a click
   * elsewhere, Escape, or its own ✕. Without it the companion keeps thinking the panel
   * is open, so the next click reads as "close" and it appears dead.
   */
  onPanelClosed(cb) {
    const handler = () => cb();
    ipcRenderer.on("crew-companion:panel-closed", handler);
    return () => ipcRenderer.removeListener("crew-companion:panel-closed", handler);
  },

  /**
   * Open the session a waiting-on-you notification is about.
   *
   * Must go through the main process for the same reason `panelOpen` does: this
   * overlay is a non-focusable full-display window with no handle on the
   * dashboard, so raising and routing the dashboard is not something it can do
   * for itself.
   *
   * `invoke`, not `send`, because the renderer needs the ANSWER: the CTA is the
   * only exit a sticky approval bubble has, so it clears the notification only
   * once the dashboard has actually been surfaced.
   *
   * @param {string} slotKey dashboard slot key, or "" when the notification
   *   names no session — the dashboard is raised, nothing is routed.
   * @returns {Promise<boolean>} whether the dashboard was surfaced.
   */
  openSession(slotKey) {
    return ipcRenderer.invoke("crew-companion:open-session", String(slotKey || ""));
  },

  /**
   * Open a link in the user's real browser.
   *
   * Must go through the main process: `window.open` from here opens another Electron
   * window, which is not what "open petdex.dev" means.
   */
  openExternal(url) {
    ipcRenderer.send("crew-companion:open-external", url);
  },

  /**
   * Tell every companion overlay the active avatar changed.
   *
   * The gallery is its OWN window, so an in-page notification never reaches the
   * overlay — which is why switching avatars appeared to do nothing until a reload.
   * The main process is the only thing both windows share.
   */
  appearanceChanged() {
    ipcRenderer.send("crew-companion:appearance-changed");
  },

  /** Fires when the active avatar changed, in any window. */
  onAppearanceChanged(cb) {
    const handler = () => cb();
    ipcRenderer.on("crew-companion:appearance-changed", handler);
    return () => ipcRenderer.removeListener("crew-companion:appearance-changed", handler);
  },

  /** Open the avatar gallery. */
  galleryOpen() {
    ipcRenderer.send("crew-companion:gallery-open");
  },

  galleryClose() {
    ipcRenderer.send("crew-companion:gallery-close");
  },

  /**
   * "Turn off companion": close the overlay immediately. The renderer sends this
   * only after the disable POST succeeds, so the app is already disabled and the
   * reconcile loop will not reopen the overlay.
   */
  turnOff() {
    ipcRenderer.send("crew-companion:turn-off");
  },

  /**
   * The avatar gallery window opened / closed.
   *
   * The overlay has no other signal — the gallery is its own window — and it needs
   * this to hold the companion still while the user is picking an avatar, then let it
   * wander again once the gallery is gone.
   */
  onGalleryOpened(cb) {
    const handler = () => cb();
    ipcRenderer.on("crew-companion:gallery-opened", handler);
    return () => ipcRenderer.removeListener("crew-companion:gallery-opened", handler);
  },

  onGalleryClosed(cb) {
    const handler = () => cb();
    ipcRenderer.on("crew-companion:gallery-closed", handler);
    return () => ipcRenderer.removeListener("crew-companion:gallery-closed", handler);
  },

  /** Which side the panel opened on, so the card can aim its entry animation. */
  onPanelOpened(cb) {
    const handler = (_e, side) => cb(side);
    ipcRenderer.on("crew-companion:panel-opened", handler);
    return () => ipcRenderer.removeListener("crew-companion:panel-opened", handler);
  },

  // ── Single-avatar / cross-display drag ──────────────────────────────────────
  // The avatar lives on one display at a time. The main process tells each overlay
  // whether it is the active one, and owns the handoff when the avatar is dragged
  // across a screen boundary (only the global cursor crosses a window edge).

  /**
   * Begin a cross-display drag. offset = cursor screen point − sprite top-left, so
   * the main process can place the avatar under the cursor as it leaves this window.
   */
  dragStart(offsetX, offsetY) {
    ipcRenderer.send("crew-companion:drag-start", offsetX | 0, offsetY | 0);
  },

  /** Explicit drag end (e.g. window blur). */
  dragEnd() {
    ipcRenderer.send("crew-companion:drag-end");
  },

  /** A mouseup this overlay saw; ends the drag wherever the cursor now is. */
  dragMouseUp() {
    ipcRenderer.send("crew-companion:drag-mouseup");
  },

  /**
   * This overlay became active or inactive. When active during a live handoff the
   * callback also receives the entry point and isDragging, so the renderer can
   * resume the drag in flight. cb(active, localX?, localY?, isDragging?).
   */
  onSetActive(cb) {
    const handler = (_e, active, x, y, isDragging) => cb(active, x, y, isDragging);
    ipcRenderer.on("crew-companion:set-active", handler);
    return () => ipcRenderer.removeListener("crew-companion:set-active", handler);
  },

  /** Per-frame avatar position while dragging on this (active) display. */
  onDragUpdate(cb) {
    const handler = (_e, x, y) => cb(x, y);
    ipcRenderer.on("crew-companion:drag-update", handler);
    return () => ipcRenderer.removeListener("crew-companion:drag-update", handler);
  },

  /** Final avatar position when the drag ends; run the edge-snap animation. */
  onDragEnded(cb) {
    const handler = (_e, x, y) => cb(x, y);
    ipcRenderer.on("crew-companion:drag-ended", handler);
    return () => ipcRenderer.removeListener("crew-companion:drag-ended", handler);
  },

  /**
   * Broadcast to every overlay at drag-start: listen for a global mouseup and
   * report it via dragMouseUp, so the drag ends even if the cursor released over a
   * different display than the one that started it.
   */
  onDragListenMouseUp(cb) {
    const handler = () => cb();
    ipcRenderer.on("crew-companion:drag-listen-mouseup", handler);
    return () => ipcRenderer.removeListener("crew-companion:drag-listen-mouseup", handler);
  },

  /**
   * Tell main this overlay's active-state listener is mounted, so main replies with
   * set-active. This is the reliable half of the activation handshake: the main
   * process's initial send may fire before this renderer subscribes.
   */
  petReady() {
    ipcRenderer.send("crew-companion:pet-ready");
  },

  /**
   * Notification OWNER role. The main process elects exactly one overlay as the
   * owner; only it runs the WebSocket + reminder poll + bubble state machine. Every
   * other overlay is a pure view. Decoupled from set-active so a drag hand-off (which
   * moves the avatar) never disturbs the producing owner.
   */
  onSetOwner(cb) {
    const handler = (_e, isOwner) => cb(Boolean(isOwner));
    ipcRenderer.on("crew-companion:set-owner", handler);
    return () => ipcRenderer.removeListener("crew-companion:set-owner", handler);
  },

  /** Owner → main: the single resolved bubble to show (or null), plus its slot snapshot and local seq. */
  reportBubbleState(bubble, slot, seq) {
    ipcRenderer.send("crew-companion:bubble-state", bubble ?? null, slot ?? null, seq ?? null);
  },

  /** Main → active overlay: render this bubble (or null). */
  onRenderBubble(cb) {
    const handler = (_e, bubble, playReaction) => cb(bubble ?? null, playReaction === true);
    ipcRenderer.on("crew-companion:render-bubble", handler);
    return () => ipcRenderer.removeListener("crew-companion:render-bubble", handler);
  },

  /** Active overlay → main → owner: a user action on the bubble (e.g. dismiss). */
  bubbleAction(action) {
    ipcRenderer.send("crew-companion:bubble-action", action || null);
  },

  /** Main → owner: a bubble action performed on the active overlay. */
  onBubbleAction(cb) {
    const handler = (_e, action) => cb(action ?? null);
    ipcRenderer.on("crew-companion:bubble-action", handler);
    return () => ipcRenderer.removeListener("crew-companion:bubble-action", handler);
  },

  /** Owner → main → active overlay: a window command (Open panel / Change avatar). */
  reportWindowCommand(cmd) {
    ipcRenderer.send("crew-companion:window-command", cmd || null);
  },

  /** Main → active overlay: run a window command at the avatar's position. */
  onWindowCommand(cb) {
    const handler = (_e, cmd) => cb(cmd ?? null);
    ipcRenderer.on("crew-companion:window-command", handler);
    return () => ipcRenderer.removeListener("crew-companion:window-command", handler);
  },

  /** Main → replacement brain: rehydrate the producer's recovery state ({slot, seq}) after a crash restart. */
  onRehydrateSlot(cb) {
    const handler = (_e, recovery) => cb(recovery ?? null);
    ipcRenderer.on("crew-companion:rehydrate-slot", handler);
    return () => ipcRenderer.removeListener("crew-companion:rehydrate-slot", handler);
  },
});
