// Native agent operations for the embedded browser panel.
//
// Once the native `WebContentsView` owns a page (browser-view.js) and LIGHT holds
// CDP control of it (browser-control.js), the agent's `browser_*` MCP calls must
// be served AGAINST THAT PAGE — never against a Playwright subprocess, which
// would silently read a different page and report success (the "no split brain"
// rule). This module is the native implementation of the wire-op vocabulary the
// Python caller speaks:
//
//   navigate, snapshot, click, type, press_key, hover, select_option,
//   screenshot, evaluate, wait_for, back, console
//
// It drives the page purely over CDP (`webContents.debugger.sendCommand`), which
// is injected as `sendCommand` so the whole module is unit-testable with a stub
// and no Electron. It maintains OUR OWN ref layer (not Playwright's): `snapshot`
// mints `eN` refs into `window.__kcRefs` and click/type/hover/select_option
// resolve them back to live elements. The pair is internally consistent, which
// is all the contract requires — the proxy never rewrites tool names.
//
// ── What is a "refusal" vs a "transport failure" here ────────────────────────
//
// Two failure layers, deliberately kept distinct so the caller can honour the
// fallback invariants (see the contract):
//
//   • TRANSPORT failure — `sendCommand` throws / the view is gone. We let it
//     propagate (throw). The command channel turns it into `ok:false` at the
//     transport layer, and only THAT is allowed to fall back to Playwright.
//   • ANSWERED refusal — the native path ran and the PAGE said no (a stale ref,
//     a thrown page exception, a wait timeout, a bad argument). We RETURN a
//     structured `{ ok:false, code, error }` object. The channel wraps it as a
//     successful result, so the caller sees a delivered answer with a
//     machine-readable `code` and must surface it — never fall back. Encoding
//     these as return values (not throws) is what keeps a revoked/again-refused
//     op from being retried on another route.
//
// Every op therefore returns `{ ok:true, ... }` on success or
// `{ ok:false, code, error }` on an answered error. Coordinates handed to CDP
// are VIEW-viewport-relative (what `getBoundingClientRect()` yields); the panel
// window offset is never added (contract invariant 1). Input is always trusted
// (`Input.dispatch*`), never `element.click()` (invariant 7).
"use strict";

// Single source of truth for "a URL the embedded view may load" (http/https
// only). Shared with browser-view.js / browser-control.js so the CDP navigation
// path cannot drift from the loadURL path. browser-view.js imports nothing from
// here, so no cycle.
const { normalizeUrl } = require("./browser-view");

/** The wire ops this module serves. Every other `browser_*` tool is the Python
 *  caller's responsibility to refuse — nothing here ever reaches Playwright. */
const WIRE_OPS = Object.freeze([
  "navigate", "snapshot", "click", "type", "press_key", "hover",
  "select_option", "screenshot", "evaluate", "wait_for", "back", "console",
]);

const NAME_CAP = 120;
const CONSOLE_CAP = 500;
const WAIT_DEFAULT_MS = 5000;
const WAIT_MAX_MS = 30000;
const WAIT_POLL_MS = 100;

// Upper bound on any single CDP round-trip. Chromium can leave a command
// unsettled forever (observed: Page.captureScreenshot against a view with no
// live compositor surface), and an unsettled command would wedge the agent's
// tool call with no recovery. Generous enough not to cut off a slow real page.
const CDP_TIMEOUT_MS = 15000;
// Bound on waiting for a navigation to actually commit (see `navigate`).
const NAV_SETTLE_MS = 10000;
const NAV_POLL_MS = 50;

// ── Pure helpers (no Electron, no CDP — unit-testable in isolation) ──────────

const REF_RE = /^e[1-9][0-9]*$/;

// CDP's accepted mouse buttons. Anything else is coerced to "left" rather than
// forwarded blindly, so a typo cannot become an unintended gesture.
const MOUSE_BUTTONS = new Set(["left", "middle", "right", "back", "forward"]);

// CDP takes modifiers as a bitmask, not names: Alt=1, Ctrl=2, Meta/Cmd=4, Shift=8.
const MODIFIER_BITS = { alt: 1, control: 2, ctrl: 2, meta: 4, command: 4, cmd: 4, shift: 8 };

/** Convert Playwright-style modifier names into CDP's bitmask. */
function modifierMask(modifiers) {
  if (!Array.isArray(modifiers)) return 0;
  let mask = 0;
  for (const m of modifiers) {
    const bit = MODIFIER_BITS[String(m || "").toLowerCase()];
    if (bit) mask |= bit;
  }
  return mask;
}

/** True for a well-formed ref minted by our walker (`e1`, `e2`, …). */
function isValidRef(ref) {
  return typeof ref === "string" && REF_RE.test(ref);
}

/** Clamp a caller-supplied integer into [min,max], falling back to `dflt`. */
function clampInt(value, min, max, dflt) {
  let n = typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : dflt;
  if (n < min) n = min;
  if (n > max) n = max;
  return n;
}

/**
 * Center of a DOMRect-like in CSS pixels, or `null` when it has no drawable
 * area. Accepts either `{x,y,width,height}` or `{left,top,width,height}`.
 */
function rectCenter(rect) {
  if (!rect || typeof rect !== "object") return null;
  const x = typeof rect.x === "number" ? rect.x : rect.left;
  const y = typeof rect.y === "number" ? rect.y : rect.top;
  const w = rect.width;
  const h = rect.height;
  if (![x, y, w, h].every((v) => typeof v === "number" && Number.isFinite(v))) return null;
  if (w === 0 && h === 0) return null;
  return { x: x + w / 2, y: y + h / 2 };
}

/**
 * Render a walker node list into OUR outline text. Stable and self-consistent,
 * deliberately NOT byte-identical to Playwright's. Each node is
 * `{ depth, role, name, ref?, state? }`; interactive nodes carry a ref, and
 * checked/disabled/expanded states become suffixes.
 */
function formatOutline(nodes) {
  if (!Array.isArray(nodes)) return "";
  return nodes
    .map((n) => {
      const indent = "  ".repeat(Math.max(0, (n && n.depth) | 0));
      const role = (n && n.role) || "generic";
      const name = n && n.name != null ? String(n.name) : "";
      let line = `${indent}- ${role} "${name}"`;
      if (n && n.ref) line += ` [ref=${n.ref}]`;
      const st = (n && n.state) || {};
      if (st.checked) line += " [checked]";
      if (st.disabled) line += " [disabled]";
      if (st.expanded === true) line += " [expanded]";
      else if (st.expanded === false) line += " [collapsed]";
      return line;
    })
    .join("\n");
}

const KEY_DEFS = {
  Enter: { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 },
  Tab: { key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 },
  Escape: { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 },
  Backspace: { key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 },
  Delete: { key: "Delete", code: "Delete", windowsVirtualKeyCode: 46 },
  ArrowUp: { key: "ArrowUp", code: "ArrowUp", windowsVirtualKeyCode: 38 },
  ArrowDown: { key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40 },
  ArrowLeft: { key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37 },
  ArrowRight: { key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39 },
  Home: { key: "Home", code: "Home", windowsVirtualKeyCode: 36 },
  End: { key: "End", code: "End", windowsVirtualKeyCode: 35 },
  PageUp: { key: "PageUp", code: "PageUp", windowsVirtualKeyCode: 33 },
  PageDown: { key: "PageDown", code: "PageDown", windowsVirtualKeyCode: 34 },
  Space: { key: " ", code: "Space", windowsVirtualKeyCode: 32 },
};

/**
 * CDP key-event fields for a named key, or `null` when unknown. A single
 * printable character maps to itself; multi-character non-named input is
 * rejected (callers should use `type` / `Input.insertText` for text).
 */
function keyDefinition(key) {
  if (typeof key !== "string" || key.length === 0) return null;
  if (KEY_DEFS[key]) return { ...KEY_DEFS[key] };
  if (key.length === 1) {
    const code = key.toUpperCase().charCodeAt(0);
    return { key, code: `Key${key.toUpperCase()}`, windowsVirtualKeyCode: code };
  }
  return null;
}

/** Human-readable description of a CDP `exceptionDetails`, never silent. */
function describeException(details) {
  if (!details) return "unknown page exception";
  const ex = details.exception;
  if (ex) {
    if (ex.description) return String(ex.description);
    if (ex.value !== undefined) return String(ex.value);
  }
  if (details.text) return String(details.text);
  return "page exception";
}

/** Flatten CDP `Runtime.consoleAPICalled` args (RemoteObjects) into text. */
function remoteArgsToText(args) {
  if (!Array.isArray(args)) return "";
  return args
    .map((a) => {
      if (!a || typeof a !== "object") return "";
      if (a.value !== undefined) return typeof a.value === "string" ? a.value : JSON.stringify(a.value);
      if (a.unserializableValue !== undefined) return String(a.unserializableValue);
      if (a.description !== undefined) return String(a.description);
      return a.type || "";
    })
    .join(" ");
}

/** Normalise a CDP console/log event into a buffer entry, or `null` to skip. */
function normalizeConsoleEvent(method, params) {
  const p = params || {};
  if (method === "Runtime.consoleAPICalled") {
    return { source: "console", level: p.type || "log", text: remoteArgsToText(p.args), ts: p.timestamp };
  }
  if (method === "Log.entryAdded" && p.entry) {
    return {
      source: "log",
      level: p.entry.level || "info",
      text: p.entry.text || "",
      ts: p.entry.timestamp,
      url: p.entry.url,
    };
  }
  return null;
}

// ── Page-side expressions (injected via Runtime.evaluate, returnByValue) ─────

/**
 * The snapshot walker. Injected into the page; maintains `window.__kcRefs`
 * (ref → element), `window.__kcRefSeq` (monotonic), `window.__kcRefDoc`, and
 * returns `{ url, title, nodes }`. The map is rebuilt when the document URI
 * changes so navigation invalidates old refs. Walks the document plus OPEN
 * shadow roots, skips invisible elements, and refs only interactive elements
 * (headings/meaningful text emit ref-less structural lines).
 */
const WALKER_SOURCE = `(() => {
  var CAP = ${NAME_CAP};
  var INTERACTIVE_TAGS = { a: true, button: true, input: true, select: true, textarea: true };
  var INTERACTIVE_ROLES = { button:1, link:1, checkbox:1, radio:1, tab:1, menuitem:1, option:1, "switch":1, combobox:1, textbox:1 };

  if (window.__kcRefDoc !== document.documentURI || !(window.__kcRefs instanceof Map)) {
    window.__kcRefs = new Map();
    window.__kcRefSeq = 0;
    window.__kcRefDoc = document.documentURI;
  }
  var refs = window.__kcRefs;

  function visible(el) {
    var style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.display === "none" || style.visibility === "hidden") return false;
    var rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    if (el.offsetParent === null && style.position !== "fixed") return false;
    return true;
  }

  function explicitRole(el) {
    var r = el.getAttribute && el.getAttribute("role");
    return r ? r.trim().toLowerCase() : "";
  }

  function roleFor(el) {
    var role = explicitRole(el);
    if (role) return role;
    var tag = el.tagName.toLowerCase();
    if (tag === "a") return el.hasAttribute("href") ? "link" : "";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      var t = (el.getAttribute("type") || "text").toLowerCase();
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "button" || t === "submit" || t === "reset") return "button";
      if (t === "hidden") return "";
      return "textbox";
    }
    if (/^h[1-6]$/.test(tag)) return "heading";
    return "";
  }

  function interactive(el, role) {
    var tag = el.tagName.toLowerCase();
    if (tag === "a") return el.hasAttribute("href");
    if (INTERACTIVE_TAGS[tag]) {
      if (tag === "input" && (el.getAttribute("type") || "").toLowerCase() === "hidden") return false;
      return true;
    }
    if (el.isContentEditable) return true;
    var ti = el.getAttribute && el.getAttribute("tabindex");
    if (ti !== null && ti !== undefined && ti !== "-1" && ti !== "") return true;
    if (role && INTERACTIVE_ROLES[role]) return true;
    return false;
  }

  function labelText(el) {
    var lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if (lb) {
      var parts = lb.split(/\\s+/).map(function (id) {
        var n = document.getElementById(id);
        return n ? (n.textContent || "").trim() : "";
      }).filter(Boolean);
      if (parts.length) return parts.join(" ");
    }
    var al = el.getAttribute && el.getAttribute("aria-label");
    if (al && al.trim()) return al.trim();
    if (el.id) {
      var sel = "label[for=\\"" + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + "\\"]";
      var lab = document.querySelector(sel);
      if (lab && (lab.textContent || "").trim()) return lab.textContent.trim();
    }
    if (el.closest) {
      var wrap = el.closest("label");
      if (wrap && (wrap.textContent || "").trim()) return wrap.textContent.trim();
    }
    var attrs = ["alt", "title", "placeholder", "value"];
    var tag = (el.tagName || "").toLowerCase();
    var itype = tag === "input" ? String(el.getAttribute("type") || "text").toLowerCase() : "";
    // Never read a secret out of the DOM into the snapshot: the outline goes
    // straight into the agent's context (and its transcript), so a populated
    // password/credential field would leak the value verbatim. Drop the value
    // attribute for those inputs and fall through to label/placeholder instead.
    var secretish =
      itype === "password" ||
      /(^|[^a-z])(password|passwd|pwd|otp|totp|mfa|cvv|cvc|secret|token|apikey)([^a-z]|$)/i.test(
        (el.getAttribute("name") || "") + " " + (el.getAttribute("id") || "") + " " +
          (el.getAttribute("autocomplete") || "")
      );
    if (secretish) {
      attrs = ["alt", "title", "placeholder"];
    }
    for (var i = 0; i < attrs.length; i++) {
      var v = el.getAttribute && el.getAttribute(attrs[i]);
      if (v && v.trim()) return v.trim();
    }
    if (secretish) return "";
    return (el.textContent || "").replace(/\\s+/g, " ").trim();
  }

  function accName(el) {
    var n = labelText(el);
    if (n.length > CAP) n = n.slice(0, CAP) + "\\u2026";
    return n;
  }

  function stateOf(el, role) {
    var st = {};
    if (el.disabled === true || (el.getAttribute && el.getAttribute("aria-disabled") === "true")) st.disabled = true;
    var ac = el.getAttribute && el.getAttribute("aria-checked");
    if (ac === "true") st.checked = true;
    else if ((role === "checkbox" || role === "radio") && el.checked === true) st.checked = true;
    var ae = el.getAttribute && el.getAttribute("aria-expanded");
    if (ae === "true") st.expanded = true;
    else if (ae === "false") st.expanded = false;
    return st;
  }

  function assignRef(el) {
    if (el.__kcRef && refs.get(el.__kcRef) === el) return el.__kcRef;
    var r = "e" + (++window.__kcRefSeq);
    try { Object.defineProperty(el, "__kcRef", { value: r, configurable: true }); }
    catch (e) { try { el.__kcRef = r; } catch (e2) {} }
    refs.set(r, el);
    return r;
  }

  var out = [];
  function emit(el, depth) {
    var role = roleFor(el);
    var name = accName(el);
    if (interactive(el, role)) {
      out.push({ depth: depth, role: role || "generic", name: name, ref: assignRef(el), state: stateOf(el, role) });
      return depth + 1;
    }
    if (role === "heading" && name) {
      out.push({ depth: depth, role: "heading", name: name });
      return depth + 1;
    }
    return depth;
  }

  function walk(root, depth) {
    var kids = root.children || [];
    for (var i = 0; i < kids.length; i++) {
      var el = kids[i];
      if (!el || el.nodeType !== 1) continue;
      var tag = el.tagName.toLowerCase();
      if (tag === "script" || tag === "style" || tag === "noscript" || tag === "template" || tag === "head") continue;
      if (!visible(el)) continue;
      var next = emit(el, depth);
      walk(el, next);
      if (el.shadowRoot) walk(el.shadowRoot, next);
    }
  }

  try {
    walk(document.body || document.documentElement, 0);
  } catch (e) {
    return { url: location.href, title: document.title, nodes: out, error: String(e && e.message) };
  }
  return { url: location.href, title: document.title, nodes: out };
})()`;

/** Resolve a ref to a live element, scroll it into view, and return its center
 *  in CSS pixels — or `{ok:false}` when the ref is stale. */
function resolveExpression(ref) {
  return `(() => {
    var el = window.__kcRefs && window.__kcRefs.get(${JSON.stringify(ref)});
    if (!el || !el.isConnected) return { ok: false };
    try { el.scrollIntoView({ block: "center", inline: "center" }); } catch (e) {}
    var r = el.getBoundingClientRect();
    if (!r || (r.width === 0 && r.height === 0)) return { ok: false };
    return { ok: true, x: r.left + r.width / 2, y: r.top + r.height / 2, tag: (el.tagName || "").toLowerCase() };
  })()`;
}

/** Focus the element a ref points at (no-op if stale). */
function focusExpression(ref) {
  return `(() => {
    var el = window.__kcRefs && window.__kcRefs.get(${JSON.stringify(ref)});
    if (!el || !el.isConnected) return { ok: false };
    try { el.focus(); } catch (e) {}
    // Report whether focus ACTUALLY landed. A disabled, readonly or otherwise
    // unfocusable target leaves focus where it was, and typing then would insert
    // into whatever the user last touched -- silently corrupting a different
    // field (a password box, another form) while reporting success.
    var active = document.activeElement === el;
    return {
      ok: true,
      focused: active,
      activeTag: (document.activeElement && document.activeElement.tagName || "").toLowerCase(),
      disabled: !!el.disabled,
      readOnly: !!el.readOnly,
    };
  })()`;
}

/** Set a `<select>`'s selected options by value/label/text. */
function selectOptionExpression(ref, values) {
  return `(() => {
    var el = window.__kcRefs && window.__kcRefs.get(${JSON.stringify(ref)});
    if (!el || !el.isConnected) return { ok: false, code: "ref_stale" };
    if ((el.tagName || "").toLowerCase() !== "select") return { ok: false, code: "not_select" };
    var want = ${JSON.stringify(values)};
    var set = {};
    for (var i = 0; i < want.length; i++) set[want[i]] = true;
    var selected = [];
    for (var j = 0; j < el.options.length; j++) {
      var opt = el.options[j];
      var match = !!(set[opt.value] || set[opt.label] || set[(opt.textContent || "").trim()]);
      opt.selected = match;
      if (match) selected.push(opt.value);
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, selected: selected };
  })()`;
}

/** True when the page's visible text currently contains `text`. */
function textProbeExpression(text) {
  return `(() => { var b = document.body; return !!(b && (b.innerText || "").indexOf(${JSON.stringify(text)}) !== -1); })()`;
}

// ── Factory ──────────────────────────────────────────────────────────────────

/**
 * Build the native op dispatcher for ONE panel's control plane.
 *
 * Deps (all injected for testability):
 *   sendCommand(method, params) -> Promise   raw CDP passthrough (LIGHT must
 *                                            already hold control — the caller
 *                                            takes the owner + runs the gate).
 *   subscribe(handler)                       OPTIONAL: register a CDP-event
 *                                            listener `(method, params)` so
 *                                            console output can be buffered.
 *   sleep(ms) / now()                        OPTIONAL: injected for wait_for
 *                                            tests; default to real timers.
 */
function createBrowserOps(deps) {
  const {
    sendCommand: rawSendCommand,
    subscribe,
    sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms))),
    now = () => Date.now(),
    commandTimeoutMs = CDP_TIMEOUT_MS,
  } = deps || {};

  if (typeof rawSendCommand !== "function") {
    throw new Error("createBrowserOps: sendCommand is required");
  }

  // A CDP command that never settles must NOT wedge the agent's tool call.
  // `Page.captureScreenshot` was observed hanging indefinitely against a view
  // with no live compositor surface -- which is a REACHABLE production state,
  // because the panel hides the view (setInactive) when the user switches side
  // -panel tabs while agent control is still granted. A bounded wait turns that
  // from an unrecoverable hang into an error the agent can act on.
  function sendCommand(method, params) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        const err = new Error(`CDP command timed out after ${commandTimeoutMs}ms: ${method}`);
        err.code = "cdp_timeout";
        reject(err);
      }, commandTimeoutMs);
      Promise.resolve()
        .then(() => rawSendCommand(method, params))
        .then(
          (v) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve(v);
          },
          (e) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            reject(e);
          }
        );
    });
  }

  const consoleBuffer = [];
  let consoleWired = false;
  let consoleEnabled = false;

  if (typeof subscribe === "function") {
    try {
      subscribe((method, params) => {
        const entry = normalizeConsoleEvent(method, params);
        if (!entry) return;
        consoleBuffer.push(entry);
        if (consoleBuffer.length > CONSOLE_CAP) {
          consoleBuffer.splice(0, consoleBuffer.length - CONSOLE_CAP);
        }
      });
      consoleWired = true;
    } catch {
      /* a missing event source must not break op dispatch */
    }
  }

  /** Evaluate an expression in the page, returning the raw CDP result. */
  async function evalInPage(expression, opts) {
    await sendCommand("Runtime.enable");
    return sendCommand("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: !!(opts && opts.awaitPromise),
    });
  }

  /** Resolve a ref to view-relative center coords, or an answered error. */
  async function resolveRef(ref) {
    if (!isValidRef(ref)) {
      return { ok: false, code: "bad_ref", error: `not a valid ref: ${JSON.stringify(ref)}` };
    }
    const res = await evalInPage(resolveExpression(ref));
    if (res && res.exceptionDetails) {
      return { ok: false, code: "resolve_error", error: describeException(res.exceptionDetails) };
    }
    const v = res && res.result ? res.result.value : null;
    if (!v || !v.ok) {
      return { ok: false, code: "ref_stale", error: `ref ${ref} is not on the current page; re-snapshot` };
    }
    return { ok: true, x: v.x, y: v.y, tag: v.tag };
  }

  async function dispatchMouseClick(x, y, opts) {
    const o = opts || {};
    const button = MOUSE_BUTTONS.has(o.button) ? o.button : "left";
    const clickCount = o.doubleClick ? 2 : 1;
    const modifiers = modifierMask(o.modifiers);
    for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
      await sendCommand("Input.dispatchMouseEvent", {
        type,
        x,
        y,
        button,
        modifiers,
        clickCount: type === "mouseMoved" ? 0 : clickCount,
      });
    }
  }

  async function dispatchKey(def) {
    await sendCommand("Input.dispatchKeyEvent", { type: "rawKeyDown", ...def });
    await sendCommand("Input.dispatchKeyEvent", { type: "keyUp", ...def });
  }

  // ── Ops ──

  /** Stamp the CURRENT document so its disappearance proves a new one committed. */
  async function stampCurrentDocument() {
    const token = `kcnav_${Math.random().toString(36).slice(2)}_${now()}`;
    try {
      await evalInPage(`window.__kcNavToken = ${JSON.stringify(token)}; 1`, { returnByValue: true });
    } catch {
      // An unstampable old document still navigates; the wait then leans on
      // readyState/href only -- weaker, but never worse than not waiting.
    }
    return token;
  }

  /**
   * Wait for a navigation to actually commit.
   *
   * Shared by `navigate` and `back` on purpose: both only START a navigation, and
   * returning early is a correctness bug rather than a race -- the next op would
   * run against the OLD document, so a snapshot would describe the previous page
   * and report success while refs minted from it still resolved.
   *
   * Two independent settle signals, because neither alone covers every case:
   *   * the stamp being gone proves a NEW document committed. readyState alone is
   *     useless here (the old document already reads "complete", so polling it
   *     settles instantly and reproduces the bug);
   *   * href matching the target covers a SAME-document navigation (e.g.
   *     "https://x/" -> "https://x/#section"), which creates no new document, so
   *     the stamp survives and waiting on it alone would stall the full timeout
   *     and then wrongly report failure for a navigation that succeeded.
   *
   * Bounded by BOTH wall clock and iteration count: `now` is an injected seam
   * (tests pin it to a constant), so a clock-only bound would never terminate.
   */
  async function waitForCommit(token, target) {
    const deadline = now() + NAV_SETTLE_MS;
    let lastState = "";
    const maxPolls = Math.max(1, Math.ceil(NAV_SETTLE_MS / NAV_POLL_MS));
    for (let poll = 0; poll < maxPolls; poll += 1) {
      if (now() > deadline) break;
      await sleep(NAV_POLL_MS);
      let probe;
      try {
        probe = await evalInPage(
          "({ s: String(document.readyState), stale: window.__kcNavToken || \"\", href: String(location.href) })",
          { returnByValue: true }
        );
      } catch {
        // Mid-navigation the execution context is torn down and re-created, so a
        // transient failure here is expected -- keep polling until the deadline.
        continue;
      }
      const val = probe && probe.result ? probe.result.value : null;
      if (!val) continue;
      lastState = `${val.s}${val.stale === token ? " (still old doc)" : ""} ${val.href}`;
      const isNewDoc = val.stale !== token;
      const hrefMatches = target ? val.href === target : false;
      if ((isNewDoc || hrefMatches) && (val.s === "interactive" || val.s === "complete")) {
        return { ok: true };
      }
    }
    return {
      ok: false,
      code: "nav_timeout",
      error: `navigation to ${target} did not commit within ${NAV_SETTLE_MS}ms (last: ${lastState})`,
    };
  }

  async function navigate(a) {
    const target = normalizeUrl(a.url);
    if (!target) {
      return { ok: false, code: "bad_url", error: `refused non-web URL: ${String(a.url).slice(0, 80)}` };
    }
    await sendCommand("Page.enable");
    const stamp = await stampCurrentDocument();
    await sendCommand("Page.navigate", { url: target });
    const committed = await waitForCommit(stamp, target);
    if (!committed.ok) return committed;
    return { ok: true, url: target };
  }

  async function back() {
    await sendCommand("Page.enable");
    const hist = await sendCommand("Page.getNavigationHistory");
    const idx = hist && typeof hist.currentIndex === "number" ? hist.currentIndex : -1;
    const entries = hist && Array.isArray(hist.entries) ? hist.entries : [];
    if (idx <= 0 || !entries[idx - 1]) {
      return { ok: false, code: "no_history", error: "no previous page in history" };
    }
    const target = entries[idx - 1];
    const stamp = await stampCurrentDocument();
    await sendCommand("Page.navigateToHistoryEntry", { entryId: target.id });
    // History navigation is as asynchronous as Page.navigate: returning here
    // would let the next op read the OLD document and report success, and refs
    // minted from it would still resolve. Wait for the destination to commit.
    const settled = await waitForCommit(stamp, target.url);
    if (!settled.ok) return settled;
    return { ok: true, url: target.url };
  }

  async function snapshot() {
    const res = await evalInPage(WALKER_SOURCE);
    if (res && res.exceptionDetails) {
      return { ok: false, code: "snapshot_error", error: describeException(res.exceptionDetails) };
    }
    const val = res && res.result ? res.result.value : null;
    if (!val || !Array.isArray(val.nodes)) {
      return { ok: false, code: "snapshot_error", error: "snapshot walker returned no data" };
    }
    return { ok: true, url: val.url, title: val.title, snapshot: formatOutline(val.nodes) };
  }

  async function click(a) {
    const r = await resolveRef(a.ref);
    if (!r.ok) return r;
    // `button`/`doubleClick`/`modifiers` MUST be honoured, not dropped. Silently
    // downgrading a right-click to a left-click would fire a destructive
    // control's primary handler instead of opening its context menu -- the wrong
    // action, reported as success.
    await dispatchMouseClick(r.x, r.y, a);
    return {
      ok: true,
      ref: a.ref,
      x: r.x,
      y: r.y,
      button: MOUSE_BUTTONS.has(a.button) ? a.button : "left",
      clickCount: a.doubleClick ? 2 : 1,
    };
  }

  async function hover(a) {
    const r = await resolveRef(a.ref);
    if (!r.ok) return r;
    await sendCommand("Input.dispatchMouseEvent", { type: "mouseMoved", x: r.x, y: r.y });
    return { ok: true, ref: a.ref, x: r.x, y: r.y };
  }

  async function typeText(a) {
    const r = await resolveRef(a.ref);
    if (!r.ok) return r;
    const f = await evalInPage(focusExpression(a.ref));
    const fv = f && f.result ? f.result.value : null;
    // Refuse unless focus demonstrably landed on the target. `Input.insertText`
    // goes to whatever holds focus, so typing without this check can write into a
    // completely different field and still report success.
    if (!fv || !fv.ok || !fv.focused) {
      return {
        ok: false,
        code: "not_focusable",
        error:
          `ref ${a.ref} did not take focus` +
          (fv && fv.disabled ? " (disabled)" : "") +
          (fv && fv.readOnly ? " (readonly)" : "") +
          (fv && fv.activeTag ? `; focus stayed on <${fv.activeTag}>` : "") +
          " -- refusing to type so a different field cannot be modified",
      };
    }
    await sendCommand("Input.insertText", { text: String(a.text || "") });
    if (a.submit) await dispatchKey(keyDefinition("Enter"));
    return { ok: true, ref: a.ref, submitted: !!a.submit };
  }

  async function pressKey(a) {
    const def = keyDefinition(String(a.key || ""));
    if (!def) return { ok: false, code: "bad_key", error: `unknown key: ${JSON.stringify(a.key)}` };
    await dispatchKey(def);
    return { ok: true, key: a.key };
  }

  async function selectOption(a) {
    if (!isValidRef(a.ref)) {
      return { ok: false, code: "bad_ref", error: `not a valid ref: ${JSON.stringify(a.ref)}` };
    }
    const values = Array.isArray(a.values)
      ? a.values.map(String)
      : a.values != null ? [String(a.values)] : [];
    const res = await evalInPage(selectOptionExpression(a.ref, values));
    if (res && res.exceptionDetails) {
      return { ok: false, code: "select_error", error: describeException(res.exceptionDetails) };
    }
    const v = res && res.result ? res.result.value : null;
    if (!v || !v.ok) {
      const code = v && v.code ? v.code : "ref_stale";
      const error = code === "not_select"
        ? `ref ${a.ref} is not a <select> element`
        : `ref ${a.ref} is not on the current page; re-snapshot`;
      return { ok: false, code, error };
    }
    return { ok: true, ref: a.ref, selected: v.selected };
  }

  async function screenshot(a) {
    const format = a.format === "jpeg" ? "jpeg" : "png";
    const params = { format };
    if (format === "jpeg" && typeof a.quality === "number") {
      params.quality = clampInt(a.quality, 0, 100, 80);
    }
    const res = await sendCommand("Page.captureScreenshot", params);
    // `mimeType` is what the caller's save-to-file path keys on (it is shared with
    // the Playwright direction, which speaks mimeType). Returning only `format`
    // would make a jpeg get saved as image/png -- a silent mismatch, since a
    // missing key just reads as absent and falls back to png.
    return {
      ok: true,
      format,
      mimeType: format === "jpeg" ? "image/jpeg" : "image/png",
      data: res && res.data ? res.data : "",
    };
  }

  async function evaluate(a) {
    const src = String(a.expression || "");
    if (!src.trim()) {
      return { ok: false, code: "bad_expression", error: "evaluate needs a function source" };
    }
    // Playwright's `browser_evaluate` passes a FUNCTION SOURCE, e.g.
    // "() => document.title" -- not a bare expression. Evaluating that text
    // directly would yield the function OBJECT (undefined under returnByValue)
    // and report success, so it must be invoked. Element-scoped calls receive
    // the resolved element as the first argument, matching Playwright, where the
    // form is "el => el.textContent".
    let callee = `(${src})`;
    if (a.ref !== undefined && a.ref !== null && a.ref !== "") {
      const r = await resolveRef(a.ref);
      if (!r.ok) return r;
      // Re-resolve the element inside the page so the call gets a live handle
      // rather than coordinates.
      const elExpr = `window.__kcRefs.get(${JSON.stringify(a.ref)})`;
      const res = await evalInPage(`(${callee})(${elExpr})`, { awaitPromise: true });
      if (res && res.exceptionDetails) {
        return { ok: false, code: "evaluate_error", error: describeException(res.exceptionDetails) };
      }
      return { ok: true, ref: a.ref, value: res && res.result ? res.result.value : undefined };
    }
    const res = await evalInPage(`(${callee})()`, { awaitPromise: true });
    if (res && res.exceptionDetails) {
      return { ok: false, code: "evaluate_error", error: describeException(res.exceptionDetails) };
    }
    return { ok: true, value: res && res.result ? res.result.value : undefined };
  }

  async function waitFor(a) {
    const timeout = clampInt(a.timeout, 0, WAIT_MAX_MS, WAIT_DEFAULT_MS);
    if (typeof a.time === "number" && a.time > 0) {
      // Playwright's `time` is in SECONDS. Treating it as milliseconds made
      // wait_for({time: 3}) sleep 3ms and report success -- a silent 1000x
      // under-wait on the op agents use to let a page settle.
      const ms = Math.min(a.time * 1000, WAIT_MAX_MS);
      await sleep(ms);
      return { ok: true, waited: ms };
    }
    const text = typeof a.text === "string" && a.text ? a.text : null;
    const gone = typeof a.textGone === "string" && a.textGone ? a.textGone : null;
    if (!text && !gone) {
      return { ok: false, code: "bad_wait", error: "wait_for needs one of: text, textGone, time" };
    }
    const target = text || gone;
    const wantPresent = !!text;
    const start = now();
    for (;;) {
      const res = await evalInPage(textProbeExpression(target));
      const present = !!(res && res.result && res.result.value);
      if (wantPresent && present) return { ok: true, matched: "present", text: target };
      if (!wantPresent && !present) return { ok: true, matched: "absent", text: target };
      if (now() - start >= timeout) {
        return {
          ok: false,
          code: "wait_timeout",
          error: `timed out after ${timeout}ms waiting for text ${wantPresent ? "present" : "absent"}: ${target}`,
        };
      }
      await sleep(WAIT_POLL_MS);
    }
  }

  async function consoleMessages(a) {
    if (!consoleEnabled) {
      await sendCommand("Runtime.enable");
      try { await sendCommand("Log.enable"); } catch { /* Log domain optional */ }
      consoleEnabled = true;
    }
    const limit = clampInt(a && a.limit, 1, CONSOLE_CAP, CONSOLE_CAP);
    return { ok: true, wired: consoleWired, messages: consoleBuffer.slice(-limit) };
  }

  const HANDLERS = {
    navigate,
    snapshot,
    click,
    type: typeText,
    press_key: pressKey,
    hover,
    select_option: selectOption,
    screenshot,
    evaluate,
    wait_for: waitFor,
    back,
    console: consoleMessages,
  };

  return {
    /**
     * Run one wire op. Unknown ops throw with the message the previous
     * `dispatchBrowserOp` used, kept byte-compatible for the caller. Answered
     * errors return `{ ok:false, code, error }`; transport errors propagate.
     */
    async run(op, args) {
      const handler = HANDLERS[op];
      if (typeof handler !== "function") {
        throw new Error(`unsupported browser control op: ${op}`);
      }
      return handler(args || {});
    },
    /** Test seam: the current console buffer length. */
    _consoleBufferSize: () => consoleBuffer.length,
  };
}

module.exports = {
  WIRE_OPS,
  NAME_CAP,
  CONSOLE_CAP,
  isValidRef,
  clampInt,
  rectCenter,
  formatOutline,
  modifierMask,
  MOUSE_BUTTONS,
  keyDefinition,
  describeException,
  remoteArgsToText,
  normalizeConsoleEvent,
  WALKER_SOURCE,
  resolveExpression,
  focusExpression,
  selectOptionExpression,
  textProbeExpression,
  createBrowserOps,
};
