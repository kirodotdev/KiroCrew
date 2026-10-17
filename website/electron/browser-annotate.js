"use strict";

// Element-level annotation for the native Browser panel.
//
// The user clicks "Annotate", then points at elements ON THE LIVE PAGE: the
// element under the cursor is highlighted (devtools-inspect style), a click
// selects it and leaves a numbered marker. The NOTE for each marker is typed in
// the panel (trusted renderer), never in the page. The pick/highlight/marker
// surface has to live INSIDE the page as an injected overlay layer, because
// the native view is composited above the dashboard's DOM -- nothing the
// renderer draws could sit on top of it.
//
// Trust posture. The page is external content. Everything the overlay reports
// -- refs, roles, names, selectors, liveness -- is page-realm data with the
// same trust as an agent `snapshot`: a hostile page can lie about what an
// element is, and the human reviews the draft before sending. What the page
// must NOT be able to observe is the user's prose, so no text input is ever
// created in the page realm: notes are typed, stored and rendered in the panel
// only, and the overlay never receives them (not even as a marker tooltip).
//
// A HUMAN action, so it deliberately bypasses the agent control plane
// (browser-control.js) and the CDP wire ops: the overlay is injected with
// `webContents.executeJavaScript`, state is read back by polling a small
// snapshot function, and the screenshot is `webContents.capturePage()`. No
// debugger attachment, no competition with the agent's single CDP owner,
// and Browser Mode may be off.
//
// Refs: the overlay splices in PAGE_HELPERS_SOURCE (browser-ops.js) -- the
// SAME `assignRef`/`accName`/`roleFor` the agent's `snapshot` walker runs,
// against the same `window.__kcRefs` map -- so the `eN` written into the chat
// draft is the `eN` the agent can pass to `click`/`hover`/`evaluate` (refs
// resolve through the map even for elements the outline does not list, e.g.
// a paragraph).
//
// The page's own DOM is never modified beyond the one host element; the
// overlay is torn down on `teardown`, and navigation drops it with the doc.

const { PAGE_HELPERS_SOURCE, ANNOTATE_HOST_ID } = require("./browser-ops");

/** Ops the renderer may ask for. A closed set, like the control ops. */
const ANNOTATE_OPS = Object.freeze(["start", "stop", "poll", "remove", "clear", "capture", "teardown"]);

/** Upper bound on one page round-trip. */
const ANNOTATE_TIMEOUT_MS = 8000;

const HOST_ID = ANNOTATE_HOST_ID;

/**
 * The overlay, installed once per document as `window.__kcAnnotate`.
 * Everything it exposes is called through `executeJavaScript` by the ops
 * below; the renderer never touches page JS directly.
 */
const OVERLAY_SOURCE = `(() => {
  if (window.__kcAnnotate && window.__kcAnnotate.doc === document && document.getElementById(${JSON.stringify(HOST_ID)})) {
    return { ok: true, reused: true };
  }
  ${PAGE_HELPERS_SOURCE}

  // A short, human-readable CSS selector for the element -- a hint for a
  // reader, NOT a stable handle (the ref is). Prefers the id, then a labelled
  // tag, then a shallow nth-of-type path. Overlay-only: the agent's walker
  // never needs it, so it is not part of the shared helpers.
  function selectorOf(el) {
    try {
      var esc = function (s) { return window.CSS && CSS.escape ? CSS.escape(s) : s; };
      if (el.id) return "#" + esc(el.id);
      var tag = el.tagName.toLowerCase();
      var attrs = ["data-testid", "name", "aria-label"];
      for (var i = 0; i < attrs.length; i++) {
        var v = el.getAttribute(attrs[i]);
        if (v && v.length <= 60) return tag + "[" + attrs[i] + "=\\"" + v.replace(/"/g, "\\\\\\"") + "\\"]";
      }
      var parts = [];
      var cur = el;
      for (var d = 0; cur && cur.nodeType === 1 && d < 4; d++) {
        var t = cur.tagName.toLowerCase();
        if (cur.id) { parts.unshift("#" + esc(cur.id)); break; }
        var p = cur.parentElement;
        if (!p) { parts.unshift(t); break; }
        var same = 0, idx = 0;
        for (var k = 0; k < p.children.length; k++) {
          if (p.children[k].tagName === cur.tagName) { same++; if (p.children[k] === cur) idx = same; }
        }
        parts.unshift(same > 1 ? t + ":nth-of-type(" + idx + ")" : t);
        cur = p;
      }
      return parts.join(" > ");
    } catch (e) {
      return "";
    }
  }

  var Z = 2147483647;
  // Marker identity colour: the dashboard's accent family, not red -- red is
  // what the panel uses for Remove and the armed Clear, and one hue must not
  // carry two meanings. Fixed hex because the page realm cannot read the
  // dashboard's theme variables.
  var COLOR = "#7c3aed";
  var editHint = "";
  var roleNames = {};
  function roleLabel(role) { return (role && Object.prototype.hasOwnProperty.call(roleNames, role) && roleNames[role]) || role; }
  var state = { picking: false, seq: 0, idSeq: 0, items: [], hover: null, picked: null, editRequest: null };

  // ── host layer (fixed, full-viewport, click-through except its own badges) ──
  var host = document.createElement("div");
  host.id = ${JSON.stringify(HOST_ID)};
  host.setAttribute("aria-hidden", "true");
  host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:" + Z + ";font:12px/1.35 system-ui,-apple-system,Segoe UI,sans-serif;color:#111;";
  var hoverBox = document.createElement("div");
  hoverBox.style.cssText = "position:absolute;display:none;box-sizing:border-box;border:2px solid " + COLOR + ";background:rgba(124,58,237,.08);border-radius:3px;pointer-events:none;transition:all .04s linear;";
  var hoverTag = document.createElement("div");
  hoverTag.style.cssText = "position:absolute;display:none;padding:2px 6px;border-radius:4px;background:" + COLOR + ";color:#fff;font-size:11px;white-space:nowrap;max-width:60vw;overflow:hidden;text-overflow:ellipsis;pointer-events:none;";
  host.appendChild(hoverBox);
  host.appendChild(hoverTag);
  // While picking, a full-viewport shield sits under the markers and above
  // the page, so every pointer event's *target* is ours: a page handler that
  // delegates by target (analytics, framework click routers) matches nothing.
  var shield = document.createElement("div");
  shield.style.cssText = "position:absolute;inset:0;pointer-events:none;cursor:crosshair;";
  host.appendChild(shield);
  var marksLayer = document.createElement("div");
  marksLayer.style.cssText = "position:absolute;inset:0;pointer-events:none;";
  host.appendChild(marksLayer);
  (document.body || document.documentElement).appendChild(host);

  function inHost(node) { return !!(node && host.contains(node)); }
  function rectOf(el) { var r = el.getBoundingClientRect(); return { x: r.left, y: r.top, width: r.width, height: r.height }; }
  function textOf(el) {
    var t = (el.textContent || "").replace(/\\s+/g, " ").trim();
    return t.length > CAP ? t.slice(0, CAP) + "\\u2026" : t;
  }
  function describe(el) {
    var role = roleFor(el);
    return { ref: assignRef(el), tag: el.tagName.toLowerCase(), role: role || "", name: accName(el), text: textOf(el), selector: selectorOf(el) };
  }
  /** Deepest element at a point, descending open shadow roots; never our own host. */
  function deepElementAt(x, y) {
    // Hit-test through the shield: elementFromPoint honours pointer-events,
    // so lifting the shield for the synchronous lookup reveals the page.
    var was = shield.style.pointerEvents;
    shield.style.pointerEvents = "none";
    var el;
    try { el = document.elementFromPoint(x, y); } finally { shield.style.pointerEvents = was; }
    if (!el || inHost(el)) return null;
    var guard = 0;
    while (el && el.shadowRoot && guard++ < 20) {
      var inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    if (!el || el === document.documentElement || el === document.body) return null;
    return el;
  }
  function itemById(id) { for (var i = 0; i < state.items.length; i++) if (state.items[i].id === id) return state.items[i]; return null; }
  /** What crosses to the panel: identity, description, liveness. No note (the
   *  panel owns those), no element handle, no rect. */
  function publicItem(it) {
    return { id: it.id, n: it.n, ref: it.ref, tag: it.tag, role: it.role, name: it.name, text: it.text, selector: it.selector, detached: !(it.el && it.el.isConnected) };
  }

  // ── hover highlight ──
  function showHover(el) {
    if (!el) { hoverBox.style.display = "none"; hoverTag.style.display = "none"; state.hover = null; return; }
    state.hover = el;
    var r = rectOf(el);
    hoverBox.style.display = "block";
    hoverBox.style.left = r.x + "px"; hoverBox.style.top = r.y + "px";
    hoverBox.style.width = r.width + "px"; hoverBox.style.height = r.height + "px";
    var role = roleFor(el); var name = accName(el);
    var tag = el.tagName.toLowerCase();
    // "button · button" says nothing twice: show the role only when it adds to the tag.
    hoverTag.textContent = tag + (role && role !== tag ? " \\u00b7 " + roleLabel(role) : "") + (name ? " \\u201c" + (name.length > 40 ? name.slice(0, 40) + "\\u2026" : name) + "\\u201d" : "");
    hoverTag.style.display = "block";
    var top = r.y - 22; if (top < 2) top = r.y + r.height + 4;
    hoverTag.style.left = Math.max(2, r.x) + "px"; hoverTag.style.top = top + "px";
  }

  // ── markers (one badge + outline per annotation) ──
  function renderMarks() {
    while (marksLayer.firstChild) marksLayer.removeChild(marksLayer.firstChild);
    for (var i = 0; i < state.items.length; i++) {
      var it = state.items[i];
      if (!it.el || !it.el.isConnected) continue;
      var r = rectOf(it.el);
      var box = document.createElement("div");
      box.style.cssText = "position:absolute;box-sizing:border-box;border:2px solid " + COLOR + ";border-radius:3px;pointer-events:none;left:" + r.x + "px;top:" + r.y + "px;width:" + r.width + "px;height:" + r.height + "px;";
      var badge = document.createElement("button");
      badge.type = "button";
      badge.setAttribute("data-kc-mark", String(it.id));
      badge.textContent = String(it.n);
      // The badge is the on-page way back into a note; say so (localized by
      // the panel, which owns the catalog).
      if (editHint) badge.title = editHint;
      badge.style.cssText = "position:absolute;pointer-events:auto;cursor:pointer;min-width:20px;height:20px;padding:0 6px;border:0;border-radius:10px;background:" + COLOR + ";color:#fff;font:600 12px/20px system-ui,sans-serif;box-shadow:0 1px 3px rgba(0,0,0,.35);left:" + Math.max(0, r.x - 10) + "px;top:" + Math.max(0, r.y - 10) + "px;";
      badge.addEventListener("click", onBadgeClick, true);
      marksLayer.appendChild(box);
      marksLayer.appendChild(badge);
    }
  }
  function onBadgeClick(e) {
    e.preventDefault(); e.stopPropagation();
    var id = Number(e.currentTarget.getAttribute("data-kc-mark"));
    if (itemById(id)) state.editRequest = id;
  }
  function removeItem(id) {
    var idx = -1;
    for (var i = 0; i < state.items.length; i++) if (state.items[i].id === id) idx = i;
    if (idx < 0) return false;
    state.items.splice(idx, 1);
    renderMarks();
    return true;
  }

  // ── pick mode ──
  // Listeners sit on window in the capture phase -- the first stop on every
  // event's path -- and stop immediate propagation, so nothing the page
  // registered on document or below runs at all. Together with the shield
  // (the event target is never a page node) the only page code that can still
  // observe a pick is a capture-phase *window* listener registered before the
  // overlay started that acts without looking at the target.
  function ownTarget(e) { return inHost(e.composedPath ? e.composedPath()[0] : e.target) && !(e.composedPath ? e.composedPath()[0] : e.target).isSameNode(shield); }
  function halt(e) { e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation(); }
  function onMove(e) {
    if (!state.picking) return;
    if (ownTarget(e)) return;
    showHover(deepElementAt(e.clientX, e.clientY));
  }
  function onClick(e) {
    if (!state.picking) return;
    if (ownTarget(e)) return;
    halt(e);
    var el = deepElementAt(e.clientX, e.clientY);
    if (!el || inHost(el)) return;
    for (var i = 0; i < state.items.length; i++) {
      if (state.items[i].el === el) { state.editRequest = state.items[i].id; return; }
    }
    var d = describe(el);
    var it = { id: ++state.idSeq, n: ++state.seq, el: el, ref: d.ref, tag: d.tag, role: d.role, name: d.name, text: d.text, selector: d.selector };
    state.items.push(it);
    state.picked = it.id;
    renderMarks();
    showHover(null);
  }
  function swallow(e) {
    if (!state.picking) return;
    if (ownTarget(e)) return;
    halt(e);
  }
  function onKey(e) {
    if (e.key === "Escape" && state.picking) { e.preventDefault(); e.stopPropagation(); setPicking(false); }
  }
  function setPicking(on) {
    on = !!on;
    if (on === state.picking) return;
    state.picking = on;
    document.documentElement.style.cursor = on ? "crosshair" : "";
    shield.style.pointerEvents = on ? "auto" : "none";
    if (!on) showHover(null);
  }
  // Pointer events fire before -- and independently of -- mouse events, so a
  // page wired to pointerdown (drag handles, sliders, canvas libs) would still
  // see the pick unless these are swallowed as well; auxclick/contextmenu/
  // dblclick cover the non-primary and repeated variants.
  var SWALLOWED = ["mousedown", "mouseup", "pointerdown", "pointerup", "auxclick", "contextmenu", "dblclick"];
  window.addEventListener("mousemove", onMove, true);
  window.addEventListener("click", onClick, true);
  for (var si = 0; si < SWALLOWED.length; si++) window.addEventListener(SWALLOWED[si], swallow, true);
  window.addEventListener("keydown", onKey, true);
  window.addEventListener("scroll", renderMarks, true);
  window.addEventListener("resize", renderMarks);
  // Layout can move elements without a scroll/resize (animations, lazy
  // content); keep markers glued at a low cadence while any exist.
  var glue = setInterval(function () { if (state.items.length) renderMarks(); }, 250);

  window.__kcAnnotate = {
    doc: document,
    // opts.seq / opts.idStart let the panel continue numbering after notes it
    // retained from a page that navigated away, so on-page markers and the
    // list agree and ids never collide with the retained ones.
    start: function (opts) {
      opts = opts || {};
      if (typeof opts.editHint === "string") editHint = opts.editHint;
      if (opts.roleNames && typeof opts.roleNames === "object") roleNames = opts.roleNames;
      if (typeof opts.seq === "number" && opts.seq > state.seq) state.seq = opts.seq;
      if (typeof opts.idStart === "number" && opts.idStart > state.idSeq) state.idSeq = opts.idStart;
      setPicking(true);
      renderMarks();
      return { ok: true, url: location.href, title: document.title };
    },
    stop: function () { setPicking(false); return { ok: true }; },
    poll: function () {
      var picked = state.picked, edit = state.editRequest;
      state.picked = null; state.editRequest = null;
      var out = { ok: true, picking: state.picking, url: location.href, title: document.title, items: state.items.map(publicItem) };
      if (picked !== null) out.picked = picked;
      if (edit !== null) out.edit = edit;
      return out;
    },
    remove: function (id) { return { ok: removeItem(Number(id)) }; },
    clear: function () { state.items = []; state.seq = 0; renderMarks(); return { ok: true }; },
    // Before a screenshot: hide the transient chrome, keep the markers.
    prepareCapture: function () { showHover(null); renderMarks(); return { ok: true, url: location.href, title: document.title }; },
    teardown: function () {
      setPicking(false); clearInterval(glue);
      window.removeEventListener("mousemove", onMove, true);
      window.removeEventListener("click", onClick, true);
      for (var ri = 0; ri < SWALLOWED.length; ri++) window.removeEventListener(SWALLOWED[ri], swallow, true);
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("scroll", renderMarks, true);
      window.removeEventListener("resize", renderMarks);
      if (host.parentNode) host.parentNode.removeChild(host);
      delete window.__kcAnnotate;
      return { ok: true };
    },
  };
  return { ok: true, reused: false };
})()`;

/** Caps applied INSIDE the page before a reply is serialized back. */
const REPLY_MAX_ITEMS = 200;
const REPLY_MAX_TEXT = 300;
const REPLY_MAX_URL = 2048;

/**
 * Call one overlay method in the page and return a BOUNDED copy of its reply;
 * `null` when the overlay is not there.
 *
 * The page owns `window.__kcAnnotate` and can replace any method, so the reply
 * is page-controlled data. Bounding happens in this expression -- not in a
 * page-defined helper the page could also replace, and not in the main
 * process after `returnByValue` has already cloned whatever the page built:
 * only the known fields cross, arrays are capped, strings are cut, and every
 * other value is dropped. A hostile page can still lie about an element; it
 * cannot make the poll loop clone unbounded data every 150ms.
 */
function callExpression(method, arg) {
  const m = JSON.stringify(method);
  const a = arg === undefined ? "" : JSON.stringify(arg);
  return `(() => {
  var api = window.__kcAnnotate;
  if (!api || typeof api[${m}] !== "function") return null;
  var r = api[${m}](${a});
  if (r === null || r === undefined) return null;
  if (typeof r !== "object") return { ok: !!r };
  var S = function (v, n) { return typeof v === "string" ? v.slice(0, n) : undefined; };
  var N = function (v) { return typeof v === "number" && isFinite(v) ? v : undefined; };
  var out = { ok: !!r.ok };
  if (typeof r.reused === "boolean") out.reused = r.reused;
  if (typeof r.picking === "boolean") out.picking = r.picking;
  var url = S(r.url, ${REPLY_MAX_URL}); if (url !== undefined) out.url = url;
  var title = S(r.title, ${REPLY_MAX_TEXT}); if (title !== undefined) out.title = title;
  var picked = N(r.picked); if (picked !== undefined) out.picked = picked;
  var edit = N(r.edit); if (edit !== undefined) out.edit = edit;
  if (Array.isArray(r.items)) {
    out.items = [];
    for (var i = 0; i < r.items.length && i < ${REPLY_MAX_ITEMS}; i++) {
      var it = r.items[i];
      if (!it || typeof it !== "object") continue;
      out.items.push({
        id: N(it.id), n: N(it.n), ref: S(it.ref, 32), tag: S(it.tag, 32), role: S(it.role, 32),
        name: S(it.name, ${REPLY_MAX_TEXT}), text: S(it.text, ${REPLY_MAX_TEXT}), selector: S(it.selector, ${REPLY_MAX_TEXT}),
        detached: !!it.detached,
      });
    }
  }
  return out;
})()`;
}

function withTimeout(promise, ms, label) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      const err = new Error(`${label} timed out after ${ms}ms`);
      err.code = "annotate_timeout";
      reject(err);
    }, ms);
    Promise.resolve(promise).then(
      (v) => { if (!settled) { settled = true; clearTimeout(timer); resolve(v); } },
      (e) => { if (!settled) { settled = true; clearTimeout(timer); reject(e); } },
    );
  });
}

function num(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** Sanitize one annotation as the page reported it: only the fields the
 *  renderer consumes, only the types it expects. */
const REF_SHAPE = /^e[1-9][0-9]*$/;
function sanitizeItem(it) {
  if (!it || typeof it !== "object") return null;
  const id = num(it.id);
  const n = num(it.n);
  // The ref goes into the draft's prose (outside the fence), so its shape is
  // pinned to what the walker mints: `e` + digits, nothing else.
  if (id === null || n === null || typeof it.ref !== "string" || !REF_SHAPE.test(it.ref)) return null;
  return {
    id,
    n,
    ref: it.ref,
    tag: typeof it.tag === "string" ? it.tag : "",
    role: typeof it.role === "string" ? it.role : "",
    name: typeof it.name === "string" ? it.name : "",
    text: typeof it.text === "string" ? it.text : "",
    selector: typeof it.selector === "string" ? it.selector : "",
    detached: !!it.detached,
  };
}

function sanitizeItems(items) {
  if (!Array.isArray(items)) return [];
  // The honest overlay mints strictly increasing ids; a duplicate can only come
  // from a page that replaced the overlay's reply. Identity must stay unique
  // here (notes are keyed by id in the panel), so a repeated id keeps its
  // first occurrence and later ones are dropped.
  const seen = new Set();
  const out = [];
  for (const raw of items) {
    const it = sanitizeItem(raw);
    if (!it || seen.has(it.id)) continue;
    seen.add(it.id);
    out.push(it);
  }
  return out;
}

/**
 * Serve one annotate op against a WebContents (or a test double exposing
 * `executeJavaScript`, `capturePage`, `isDestroyed`). Answered failures
 * resolve `{ ok:false, code, error }`; unknown ops throw like the control
 * dispatcher does.
 *
 * `poll` may carry `picked` (an element was just selected -- the panel opens
 * its note editor) or `edit` (a marker was clicked). Both are one-shot.
 */
async function runAnnotateOp(webContents, op, args) {
  if (!ANNOTATE_OPS.includes(op)) throw new Error(`unsupported annotate op: ${op}`);
  const wc = webContents;
  if (!wc || typeof wc.executeJavaScript !== "function") {
    return { ok: false, code: "no_view", error: "no native browser view" };
  }
  if (typeof wc.isDestroyed === "function" && wc.isDestroyed()) {
    return { ok: false, code: "no_view", error: "the browser view is gone" };
  }
  const a = args && typeof args === "object" ? args : {};
  // No `userGesture`: the page can reassign any `window.__kcAnnotate` method,
  // and a 150ms poll loop that ran page code WITH user activation would hand a
  // hostile page an unbounded supply of activation-gated calls (window.open,
  // popups). Nothing the overlay does needs activation.
  const exec = (src, label) => withTimeout(wc.executeJavaScript(src), ANNOTATE_TIMEOUT_MS, label);
  try {
    switch (op) {
      case "start": {
        const installed = await exec(OVERLAY_SOURCE, "overlay install");
        if (!installed || !installed.ok) return { ok: false, code: "install_failed", error: "could not install the annotate overlay" };
        const opts = {};
        if (typeof a.editHint === "string") opts.editHint = a.editHint;
        // Plain role names for the hover tag: a closed string->string map,
        // bounded per entry, so the panel's catalog reaches the page realm
        // without opening it to arbitrary payloads.
        if (a.roleNames && typeof a.roleNames === "object" && !Array.isArray(a.roleNames)) {
          const names = {};
          for (const k of Object.keys(a.roleNames).slice(0, 32)) {
            if (/^[a-z]{1,32}$/.test(k) && typeof a.roleNames[k] === "string") names[k] = a.roleNames[k].slice(0, 64);
          }
          opts.roleNames = names;
        }
        if (num(a.seq) !== null) opts.seq = a.seq;
        if (num(a.idStart) !== null) opts.idStart = a.idStart;
        const res = await exec(callExpression("start", opts), "annotate start");
        if (!res || !res.ok) return { ok: false, code: "no_overlay", error: "the annotate overlay did not start" };
        return { ok: true, url: String(res.url || ""), title: String(res.title || "") };
      }
      case "stop":
        return (await exec(callExpression("stop"), "annotate stop")) ? { ok: true } : { ok: false, code: "no_overlay", error: "no annotate overlay on this page" };
      case "poll": {
        const res = await exec(callExpression("poll"), "annotate poll");
        if (!res) return { ok: false, code: "no_overlay", error: "no annotate overlay on this page" };
        const out = {
          ok: true,
          picking: !!res.picking,
          url: String(res.url || ""),
          title: String(res.title || ""),
          items: sanitizeItems(res.items),
        };
        if (num(res.picked) !== null) out.picked = res.picked;
        if (num(res.edit) !== null) out.edit = res.edit;
        return out;
      }
      case "remove": {
        const id = num(a.id);
        if (id === null) return { ok: false, code: "bad_id", error: "remove needs a numeric id" };
        const res = await exec(callExpression("remove", id), "annotate remove");
        return res ? { ok: !!res.ok } : { ok: false, code: "no_overlay", error: "no annotate overlay on this page" };
      }
      case "clear":
        return (await exec(callExpression("clear"), "annotate clear")) ? { ok: true } : { ok: false, code: "no_overlay", error: "no annotate overlay on this page" };
      case "teardown":
        await exec(callExpression("teardown"), "annotate teardown");
        return { ok: true };
      case "capture": {
        if (typeof wc.capturePage !== "function") return { ok: false, code: "no_view", error: "this view cannot be captured" };
        const prep = await exec(callExpression("prepareCapture"), "annotate prepare");
        if (!prep) return { ok: false, code: "no_overlay", error: "no annotate overlay on this page" };
        const image = await withTimeout(wc.capturePage(), ANNOTATE_TIMEOUT_MS, "capturePage");
        if (!image || typeof image.toPNG !== "function") return { ok: false, code: "capture_failed", error: "capturePage returned no image" };
        const size = typeof image.getSize === "function" ? image.getSize() : { width: 0, height: 0 };
        if (!size.width || !size.height) return { ok: false, code: "capture_empty", error: "the page has not painted yet -- try again" };
        return {
          ok: true,
          png: image.toPNG().toString("base64"),
          url: String(prep.url || ""),
          title: String(prep.title || ""),
        };
      }
      default:
        throw new Error(`unsupported annotate op: ${op}`);
    }
  } catch (e) {
    if (e && /unsupported annotate op/.test(String(e.message))) throw e;
    const code = e && e.code === "annotate_timeout" ? "annotate_timeout" : "annotate_failed";
    return { ok: false, code, error: String((e && e.message) || e) };
  }
}

module.exports = {
  ANNOTATE_OPS,
  HOST_ID,
  REPLY_MAX_ITEMS,
  REPLY_MAX_TEXT,
  OVERLAY_SOURCE,
  callExpression,
  sanitizeItem,
  sanitizeItems,
  runAnnotateOp,
};
