// Agent control plane for the native browser panel.
//
// The panel separates DISPLAY from CONTROL (design doc §3). browser-view.js owns
// display — the embedded `WebContentsView` and its geometry. This module owns
// control: who, if anyone, is currently driving that page on the agent's behalf.
//
// ── Owners ───────────────────────────────────────────────────────────────────
//
//   NONE        no agent attachment. The human still drives the page with real
//               OS input, which never goes through CDP and so never conflicts.
//   LIGHT       in-process `webContents.debugger` speaking CDP 1.3. No debug
//               port, no external surface. Proven sufficient for the embedded
//               view: Runtime.evaluate plus Input.dispatchMouseEvent (§12).
//   PLAYWRIGHT  a SEPARATE browser process driven by Playwright — in practice
//               the browser `@playwright/mcp` already launches for the agent.
//
// ── Why PLAYWRIGHT needs NO debug port ───────────────────────────────────────
//
// The design originally had this mode open `--remote-debugging-port` and
// `connectOverCDP` onto the DASHBOARD's own Electron process, to drive the
// embedded view with Playwright. The Stage 2 spike killed that: a port on this
// process enumerates the dashboard itself as a drivable CDP target (§12), handing
// out the surface that holds the session cookie.
//
// The accepted resolution — PLAYWRIGHT owns a separate browser — removes the
// need for a port entirely. Playwright launches and controls its own browser over
// its own pipe; `connectOverCDP` is only for attaching to a browser someone ELSE
// launched. So this module deliberately exposes NO port lifecycle: acquiring
// PLAYWRIGHT means "an external browser session is now the agent's target", and
// the only thing that has to happen locally is that LIGHT lets go of the
// embedded view.
//
// Do not reintroduce a debug port here. It would add back exactly the exposure
// the separate-process decision removed, for no capability gain.
//
// ── The single-owner rule, and what actually enforces it ─────────────────────
//
// Exactly one agent owner at a time; hand off, never overlap. Two facts from the
// Stage 2 spike shape how this is implemented:
//
//   • A second `webContents.debugger.attach()` on the same target DOES throw
//     ("Debugger is already attached to the target"), so double-attach within
//     LIGHT is caught by the platform.
//   • An external CDP client does NOT conflict with the in-process debugger —
//     Playwright evaluated the same page while `webContents.debugger` was still
//     attached, because Chromium hands external clients their own sessions.
//
// So the platform does NOT enforce single-owner across owner classes. This
// module is the enforcement point: every transition detaches the current owner
// before the next one is allowed to take over. Treat the invariant as ours to
// keep, not something we can lean on Chromium for.
//
// ── Coordinates ──────────────────────────────────────────────────────────────
//
// `Input.dispatchMouseEvent` coordinates are relative to the VIEW's own
// viewport, not the window. The spike measured a button center at {x:230,y:409}
// for a panel positioned at window x=820. Never add the panel offset.
"use strict";

// `normalizeUrl` is the single definition of "a URL the embedded view may load"
// (http/https only; file:, data:, javascript: and friends refused). Imported
// rather than re-implemented so the CDP navigation path and the loadURL path can
// never drift apart. browser-view.js imports nothing from here, so no cycle.
const { normalizeUrl } = require("./browser-view");

/** Agent control owners. */
const OWNER = Object.freeze({
  NONE: "none",
  LIGHT: "light",
  PLAYWRIGHT: "playwright",
});

const CDP_VERSION = "1.3";

/** Every owner value, for validation. */
const OWNERS = Object.freeze([OWNER.NONE, OWNER.LIGHT, OWNER.PLAYWRIGHT]);

/**
 * Plan the transition from `current` to `requested`.
 *
 * Pure, so the invariant is testable without a live window. Returns the ordered
 * steps a caller must perform, or `{ invalid: true }` for an unknown owner.
 *
 * The shape encodes the rule: `detachLight` always precedes `attachLight`, and
 * the external session is only ever claimed while PLAYWRIGHT owns control.
 */
function planTransition(current, requested) {
  if (!OWNERS.includes(current) || !OWNERS.includes(requested)) {
    return { invalid: true };
  }
  if (current === requested) {
    return {
      noop: true,
      detachLight: false,
      attachLight: false,
      acquireExternal: false,
      releaseExternal: false,
    };
  }
  return {
    noop: false,
    // Releasing the in-process debugger is required whenever LIGHT is losing
    // control — including on the way to PLAYWRIGHT, which is the overlap the
    // platform would otherwise allow.
    detachLight: current === OWNER.LIGHT,
    attachLight: requested === OWNER.LIGHT,
    acquireExternal: requested === OWNER.PLAYWRIGHT,
    releaseExternal: current === OWNER.PLAYWRIGHT,
  };
}

/**
 * May the agent take control right now?
 *
 * Agent control is UNATTENDED action, so it is gated on the app's general
 * "let the agent act" authorization — the same gate that authorizes agent
 * browsing today, not a new browser-specific toggle (design decision §8).
 * A human driving the page needs no gate: their gesture is the consent.
 */
function canAgentControl(state) {
  const s = state || {};
  if (!s.agentActEnabled) return { allowed: false, reason: "agent-act-not-authorized" };
  if (!s.viewOpen) return { allowed: false, reason: "no-browser-view" };
  return { allowed: true, reason: null };
}

/**
 * Which control transport should serve the agent's `browser_*` calls?
 *
 * Mirrors the display-transport rule in the panel: if frames are streaming, the
 * browser lives in another process (remote gateway / Playwright proxy) and only
 * the proxy can drive it. Otherwise, when a native view is available in this
 * process, drive it in-process over CDP.
 */
function chooseControlTransport(state) {
  const s = state || {};
  if (s.framesStreaming) return "proxy";
  return s.nativeAvailable ? "native" : "proxy";
}

/**
 * Build the control plane for one embedded browser view.
 *
 * Injected deps keep this testable with no Electron:
 *   getWebContents() -> the view's webContents (or null when closed)
 *   onAudit(event, detail) -> security-audit sink for owner transitions
 *   acquireExternalSession() / releaseExternalSession() -> claim/release the
 *     external Playwright-driven browser. NOT a debug port (see header).
 */
function createControlPlane(deps) {
  const {
    getWebContents,
    onAudit = () => {},
    acquireExternalSession = async () => null,
    releaseExternalSession = async () => {},
  } = deps || {};

  let owner = OWNER.NONE;
  let attached = false;
  // The exact webContents LIGHT's debugger is attached to. Tracked so a stale
  // attachment can be told apart from a live one: the embedded view can be torn
  // down and rebuilt under us (panel reopened, session re-keyed), leaving the
  // owner recorded as LIGHT while `getWebContents()` returns a fresh target the
  // debugger was never attached to. Compared by identity, never dereferenced.
  let attachedWc = null;

  const audit = (event, detail) => {
    try {
      onAudit(event, detail);
    } catch {
      /* an audit sink must never break a transition */
    }
  };

  function debuggerFor() {
    const wc = getWebContents();
    if (!wc || !wc.debugger) return null;
    return wc.debugger;
  }

  function detachLight() {
    const dbg = debuggerFor();
    attached = false;
    attachedWc = null;
    if (!dbg) return;
    try {
      if (typeof dbg.isAttached === "function" ? dbg.isAttached() : true) {
        dbg.detach();
      }
    } catch {
      // Already gone (view destroyed mid-transition). The invariant we care
      // about is "not holding it", which is now true either way.
    }
  }

  function attachLight() {
    const wc = getWebContents();
    const dbg = wc && wc.debugger ? wc.debugger : null;
    if (!dbg) throw new Error("no browser view to control");
    // Idempotent: re-attaching when we already hold it would throw, and a
    // transition must be safe to re-run.
    if (typeof dbg.isAttached === "function" && dbg.isAttached()) {
      attached = true;
      attachedWc = wc;
      return;
    }
    dbg.attach(CDP_VERSION);
    attached = true;
    attachedWc = wc;
  }

  // True when LIGHT's in-process debugger is still attached to the CURRENT
  // embedded view. Goes false when the WebContentsView is rebuilt underneath us:
  // the owner is still recorded as LIGHT, but `getWebContents()` now returns a
  // different, un-attached target. This is what lets a re-request of LIGHT know
  // it must re-attach rather than short-circuit as a no-op.
  function lightAttachmentLive() {
    if (owner !== OWNER.LIGHT || !attached) return false;
    const wc = getWebContents();
    if (!wc || !wc.debugger) return false;
    if (attachedWc && wc !== attachedWc) return false;
    const dbg = wc.debugger;
    if (typeof dbg.isAttached === "function" && !dbg.isAttached()) return false;
    return true;
  }

  async function send(method, params) {
    if (owner !== OWNER.LIGHT || !attached) {
      throw new Error(`cannot send ${method}: LIGHT does not hold control`);
    }
    const dbg = debuggerFor();
    if (!dbg) throw new Error(`cannot send ${method}: view is gone`);
    return dbg.sendCommand(method, params || {});
  }

  return {
    getOwner: () => owner,
    isAttached: () => attached,

    /**
     * Move control to `requested`, enforcing the single-owner rule.
     * `gate` is the result of canAgentControl(); an unauthorized request is
     * refused without touching the current owner.
     */
    async setOwner(requested, gate) {
      const plan = planTransition(owner, requested);
      if (plan.invalid) throw new Error(`unknown control owner: ${requested}`);

      // The gate is evaluated BEFORE the no-op short-circuit, deliberately.
      // Re-requesting the owner you already hold is a no-op transition, but it
      // is still the moment a caller asks "may I act?" — and callers use exactly
      // that call as their permission check. Short-circuiting first meant a
      // revoked authorization was never consulted again, so an agent that had
      // already attached kept driving after the user turned "let the agent act"
      // off. Order matters more than the redundant work it avoids.
      if (requested !== OWNER.NONE) {
        const verdict = gate || { allowed: false, reason: "no-gate-supplied" };
        if (!verdict.allowed) {
          audit("browser-control-refused", { requested, reason: verdict.reason });
          return { owner, changed: false, refused: verdict.reason };
        }
      }
      if (plan.noop) {
        // Re-requesting the owner already held is normally a no-op. The one
        // exception: LIGHT whose attachment went stale because the embedded view
        // was rebuilt under us. The owner is still LIGHT, but the debugger is now
        // attached to nothing (or to a target that no longer exists), so the next
        // CDP send would hit a detached target — the bug that forced users to
        // fully close the browser before a second agent op. Re-attach to the
        // current view instead of short-circuiting.
        if (requested === OWNER.LIGHT && !lightAttachmentLive()) {
          detachLight();
          try {
            attachLight();
          } catch (err) {
            owner = OWNER.NONE;
            audit("browser-control-failed", { requested, error: String(err && err.message) });
            throw err;
          }
          audit("browser-control-reattach", { owner });
          return { owner, changed: false, reattached: true };
        }
        return { owner, changed: false };
      }

      // Order matters: release before acquire, so the two owner classes never
      // overlap even though Chromium would permit it.
      if (plan.detachLight) detachLight();
      if (plan.releaseExternal) await releaseExternalSession();

      let session = null;
      try {
        if (plan.acquireExternal) session = await acquireExternalSession();
        if (plan.attachLight) attachLight();
      } catch (err) {
        // A failed acquire must not leave a phantom owner recorded.
        if (plan.acquireExternal) await releaseExternalSession();
        owner = OWNER.NONE;
        audit("browser-control-failed", { requested, error: String(err && err.message) });
        throw err;
      }

      const previous = owner;
      owner = requested;
      audit("browser-control-owner", { from: previous, to: owner, session });
      return { owner, changed: true, session };
    },

    /** Release any agent control. Always safe to call. */
    async release() {
      return this.setOwner(OWNER.NONE, { allowed: true, reason: null });
    },

    /** Raw CDP passthrough — only valid while LIGHT holds control. */
    send,

    // ── High-level operations, expressed so the existing `browser_*` tool
    // surface can be served without the agent learning new verbs. ──

    async navigate(url) {
      // CDP `Page.navigate` does NOT emit `will-navigate`, so browser-view.js's
      // navigation guard cannot see it. Without normalizing here, an agent-
      // supplied `file:///...` would load a local file into the embedded view,
      // and `evaluate`/`snapshot` below would then read its contents straight
      // back out. Normalize at this chokepoint so EVERY caller of the control
      // plane is covered, not just the IPC dispatcher.
      const target = normalizeUrl(url);
      if (!target) throw new Error(`refused navigation to a non-web URL: ${String(url).slice(0, 60)}`);
      await send("Page.enable");
      return send("Page.navigate", { url: target });
    },

    async evaluate(expression) {
      await send("Runtime.enable");
      const res = await send("Runtime.evaluate", { expression, returnByValue: true });
      return res && res.result ? res.result.value : undefined;
    },

    /** Click at VIEW-relative coordinates (never window-relative — see header). */
    async click(x, y) {
      for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
        await send("Input.dispatchMouseEvent", {
          type,
          x,
          y,
          button: "left",
          clickCount: type === "mouseMoved" ? 0 : 1,
        });
      }
    },

    async type(text) {
      for (const ch of String(text)) {
        await send("Input.dispatchKeyEvent", { type: "char", text: ch });
      }
    },

    /** Accessibility tree — the cheap page representation the agent reads. */
    async snapshot() {
      await send("Accessibility.enable");
      return send("Accessibility.getFullAXTree");
    },
  };
}

module.exports = {
  OWNER,
  OWNERS,
  CDP_VERSION,
  planTransition,
  canAgentControl,
  chooseControlTransport,
  createControlPlane,
};
