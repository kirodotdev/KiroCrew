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

// ── Op classes: what the Globe actually authorizes ───────────────────────────
//
// The embedded browser is the DEFAULT renderer for a page the user asked for,
// so the two op classes answer two different questions.
//
//   VIEW     change what the user SEES. `navigate` here is the SAME
//            `manager.navigate` the human's own panel controls call, with the
//            same http/https-only guard, so a chat "open this page" is the
//            user's attended request expressed a different way. Gating it would
//            buy nothing — the identical end state is one ungated click away —
//            while costing the native view and silently downgrading the user to
//            the mirror transport.
//   OPERATE  act on the page, or READ ITS CONTENT BACK. This is the unattended
//            agent action design decision §8 is about, and it stays gated on
//            "let the agent act" (the Globe).
//
// Reading is deliberately OPERATE, not VIEW, and the reason is the partition.
// The embedded view runs on `persist:` (browser-view.js), which holds sessions
// the user logged into BY HAND — so `snapshot`/`screenshot`/`console` do not just
// show a page, they lift authenticated content into model context. The human
// clicking a URL has no equivalent: they read the page with their own eyes, and
// nothing leaves the machine. The Playwright fallback never had this reach either
// (it runs isolated with an empty storage state), so an ungated read here would
// be a NEW capability, reachable by prompt injection in content already read.
// Showing the user a page does not require handing its contents to the agent, so
// the native-default goal survives this split intact.
//
// Reclassifying an op is a security decision, not a taste one: anything that can
// change page state on the user's behalf, execute attacker-reachable script, or
// return page content to the agent belongs in OPERATE.
const AGENT_VIEW_OPS = Object.freeze([
  "navigate",
  "back",
  "wait_for",
]);

const AGENT_OPERATE_OPS = Object.freeze([
  "click",
  "type",
  "press_key",
  "hover",
  "select_option",
  "evaluate",
  // Content reads — gated for the partition reason above, not because they mutate.
  "snapshot",
  "screenshot",
  "console",
]);

const _VIEW_OP_SET = new Set(AGENT_VIEW_OPS);

/**
 * Classify one agent op as "view" or "operate".
 *
 * Unknown ops are treated as "operate" — fail CLOSED. A verb added to the
 * Electron dispatcher without being classified here must not silently inherit
 * the ungated path.
 */
function agentOpClass(op) {
  return _VIEW_OP_SET.has(String(op || "")) ? "view" : "operate";
}

/**
 * May the agent take control right now, for an op of this class?
 *
 * `opClass` defaults to "operate" so every existing caller keeps the stricter
 * behaviour unless it opts in by naming the class.
 *
 * A human driving the page needs no gate at all: their gesture is the consent,
 * and the renderer's own panel controls never consult this function.
 */
function canAgentControl(state, opClass = "operate") {
  const s = state || {};
  if (opClass !== "view" && !s.agentActEnabled) {
    return { allowed: false, reason: "agent-act-not-authorized" };
  }
  if (!s.viewOpen) return { allowed: false, reason: "no-browser-view" };
  return { allowed: true, reason: null };
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
    const dbg = debuggerFor();
    if (!dbg) throw new Error("no browser view to control");
    // Idempotent: re-attaching when we already hold it would throw, and a
    // transition must be safe to re-run.
    if (typeof dbg.isAttached === "function" && dbg.isAttached()) {
      attached = true;
      return;
    }
    dbg.attach(CDP_VERSION);
    attached = true;
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
      if (plan.noop) return { owner, changed: false };

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
  AGENT_VIEW_OPS,
  AGENT_OPERATE_OPS,
  agentOpClass,
  planTransition,
  canAgentControl,
  createControlPlane,
};
