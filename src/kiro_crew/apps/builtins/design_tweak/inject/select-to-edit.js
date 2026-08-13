/*
 * Select-to-Edit — drop-in selection + in-place comment overlay.
 *
 * Runs inside the previewed app's own page (same-origin). Lets you right-click
 * an element and leave a comment like a Figma pin; the comment becomes an edit
 * request, and the agent's progress streams back into a thread popover anchored
 * to the element.
 *
 * Delivery model (embedded in the Design Tweak preview iframe):
 *   - The overlay NEVER calls the backend directly (the gateway API requires an
 *     auth token only the panel's SDK has). Instead it talks to the parent panel
 *     over postMessage; the panel owns all backend reads/writes.
 *   - Overlay → panel:  { source:'kiro-select-to-edit', type, ... }
 *       type 'capture'  → new comment  { clientRef, payload }
 *       type 'dispatch' → (re)send to agent  { id, text? }
 *   - panel → overlay:  { source:'kiro-ste-host', type, ... }
 *       type 'state'    → { editMode, theme }
 *       type 'created'  → { clientRef, id, number, status, thread }
 *       type 'requests' → { items:[{id,number,status,comment,element,locator,thread}] }
 *       type 'focus'    → { id }   (open a pin's thread; from the left rail)
 *
 * TRUST BARRIER ON THIS CHANNEL
 *   Inbound: a message is acted on only when it came from THIS frame's embedder
 *   (`event.source === window.parent`) AND carries an allowlisted `type`. Window
 *   identity rather than origin, because the overlay cannot know the panel's
 *   origin up front — it cannot read `parent.location` — so it LEARNS it from the
 *   first message that passes that gate, the same way McpAppFrame and
 *   useCommentBridge authenticate their frames host-side.
 *   Outbound: every post targets that learned origin. There is NO '*' wildcard —
 *   the preview is served from one of the app's own loopback origins, never the
 *   dashboard's, so a concrete target always exists. Anything produced before the
 *   handshake is queued and flushed once the origin is known.
 *
 * Standalone (not embedded): set window.__KIRO_STE__ = { backend:"…/api" } and
 * it POSTs /submit directly (no pins/thread — capture-only fallback).
 */
(function () {
  "use strict";
  if (window.__KIRO_STE_LOADED__) return;
  window.__KIRO_STE_LOADED__ = true;

  var CFG = window.__KIRO_STE__ || {};
  var PAGE_PIN_INSET = 16;   // bottom-left home for an unplaceable pin
  var SNIPPET_MAX = 600;

  var state = { active: false, hover: null, selected: null };

  var EMBEDDED = false;
  try { EMBEDDED = window.parent && window.parent !== window; } catch (_) { EMBEDDED = true; }

  var THEME = {
    accent: "#8b5cf6", accentFg: "#ffffff", panel: "#141220", card: "#1b1830",
    bgElevated: "#221f38", text: "#e9e7ff", textStrong: "#ffffff", muted: "#9d99b7",
    border: "#2a2740", info: "#3b82f6", ok: "#22c55e", warn: "#f59e0b",
  };

  // ---- overlay highlight boxes ----
  var hoverBox = mkBox(THEME.info, "rgba(59,130,246,0.12)");
  var selBox = mkBox(THEME.accent, "rgba(139,92,246,0.18)");
  selBox.style.display = "none";
  hoverBox.style.display = "none";

  var toggleBtn = document.createElement("button");
  toggleBtn.textContent = "◎ Select to Edit";
  css(toggleBtn, {
    position: "fixed", zIndex: 2147483646, right: "16px", bottom: "16px",
    padding: "8px 12px", borderRadius: "8px", border: "1px solid " + THEME.accent,
    background: "#1e1b2e", color: "#e9e7ff", font: "600 12px system-ui, sans-serif",
    cursor: "pointer", boxShadow: "0 4px 14px rgba(0,0,0,.35)",
  });
  toggleBtn.addEventListener("click", function () { setActive(!state.active); });

  var input = null;        // floating NEW-comment composer
  var popover = null;      // open thread popover (existing request)
  var popoverId = null;    // id of the request whose thread is open
  var popoverSig = null;   // signature of the last-rendered thread (skip no-op redraws)

  // pins: id -> { id, item, el (target), dot }
  var pins = Object.create(null);
  var pinLayer = document.createElement("div");
  css(pinLayer, { position: "fixed", left: 0, top: 0, width: 0, height: 0, zIndex: 2147483644, display: "none" });

  function mount() {
    document.body.appendChild(hoverBox);
    document.body.appendChild(selBox);
    document.body.appendChild(pinLayer);
    if (!EMBEDDED) document.body.appendChild(toggleBtn);
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);

  // ---- mode toggle ----
  function setActive(on) {
    state.active = on;
    toggleBtn.style.background = on ? THEME.accent : "#1e1b2e";
    toggleBtn.style.color = on ? "#fff" : "#e9e7ff";
    hoverBox.style.display = "none";
    // Pins + thread popovers are an Edit-mode affordance only.
    pinLayer.style.display = on ? "block" : "none";
    if (!on) { clearSelection(); closeThread(); }
  }

  document.addEventListener("keydown", function (e) {
    if (e.altKey && (e.key === "s" || e.key === "S")) {
      e.preventDefault();
      setActive(!state.active);
    } else if (e.key === "Escape") {
      if (popover) closeThread();
      else if (state.selected) clearSelection();
      else if (state.active) setActive(false);
    }
  });

  function applyTheme(t) {
    if (!t) return;
    for (var k in THEME) if (t[k]) THEME[k] = t[k];
    hoverBox.style.borderColor = THEME.info;
    selBox.style.borderColor = THEME.accent;
  }

  // ---- host → overlay messages ----
  //
  // Two gates, in order:
  //   1. WINDOW IDENTITY — the message must come from this frame's embedder.
  //      Origin is not the gate here because the overlay cannot know the panel's
  //      origin a priori — it cannot read `parent.location`, and the page it runs
  //      in is an arbitrary user project. It LEARNS that origin from the first
  //      message that passes this gate (see HOST_ORIGIN) and pins it thereafter.
  //      Any other window holding a handle to this frame (an opener, a nested
  //      frame) is refused.
  //   2. TYPE ALLOWLIST — an unknown `type` is dropped rather than falling
  //      through to the `type === undefined` legacy branch, which would let a
  //      stray object repaint the theme and flip edit mode.
  var HOST_TYPES = { state: 1, created: 1, requests: 1, focus: 1, toggle: 1 };

  // Learned from the first message that passes both gates, so EVERY outbound post
  // names a real origin — the overlay never uses a '*' wildcard. Kept null until
  // then, and never set to the literal "null" an opaque-origin sender reports
  // (no targetOrigin can match that, and posting it throws).
  var HOST_ORIGIN = null;

  // Outbound messages produced before the handshake completed. The panel posts
  // `state` + `requests` on every iframe load, so this is normally empty; it
  // exists because Alt+S can arm edit mode without the panel having spoken yet,
  // and dropping that first comment would lose real user input. Bounded, so a
  // frame that never gets a host message cannot grow this without limit.
  var PENDING_OUT = [];
  var PENDING_MAX = 20;

  function isHostWindow(src) {
    try { return src === window.parent; } catch (_) { return false; }
  }

  /**
   * Post up to the panel, always at its real origin.
   *
   * There is no wildcard path. The preview is served from one of the app's own
   * loopback origins (never the dashboard's), so a concrete target always exists
   * — the overlay just has to have heard from the panel once to know it. Anything
   * produced before that is queued and flushed on the handshake, so the payload
   * is never broadcast to whatever document happens to be the embedder.
   */
  function postToHost(msg) {
    if (!EMBEDDED) return;
    if (!HOST_ORIGIN) {
      if (PENDING_OUT.length < PENDING_MAX) PENDING_OUT.push(msg);
      return;
    }
    try {
      window.parent.postMessage(msg, HOST_ORIGIN);
    } catch (_) { /* embedder gone */ }
  }

  function flushPendingOut() {
    if (!HOST_ORIGIN || !PENDING_OUT.length) return;
    var queued = PENDING_OUT;
    PENDING_OUT = [];
    for (var i = 0; i < queued.length; i++) {
      try { window.parent.postMessage(queued[i], HOST_ORIGIN); } catch (_) { return; }
    }
  }

  window.addEventListener("message", function (e) {
    if (!isHostWindow(e && e.source)) return;
    var d = e && e.data;
    if (!d || d.source !== "kiro-ste-host") return;
    if (d.type !== undefined && !HOST_TYPES[d.type]) return;
    // Pin the panel's origin from the first message that got this far, then
    // release anything the overlay produced before the handshake.
    if (!HOST_ORIGIN && typeof e.origin === "string" && /^https?:\/\//.test(e.origin)) {
      HOST_ORIGIN = e.origin;
      flushPendingOut();
    }
    if (d.type === "state" || d.type === undefined) {
      applyTheme(d.theme);
      if (typeof d.editMode === "boolean") setActive(d.editMode);
    } else if (d.type === "created") {
      onCreated(d);
    } else if (d.type === "requests") {
      reconcile(Array.isArray(d.items) ? d.items : []);
    } else if (d.type === "focus") {
      if (pins[d.id]) { scrollPinIntoView(pins[d.id]); openThread(d.id); }
    } else if (d.type === "toggle") {
      if (popoverId === d.id) closeThread();
      else if (pins[d.id]) { scrollPinIntoView(pins[d.id]); openThread(d.id); }
    }
  });

  // ---- hover ----
  document.addEventListener("mousemove", function (e) {
    if (!state.active || state.selected) return;
    var el = elementAt(e);
    if (!el || el === state.hover) return;
    state.hover = el;
    positionBox(hoverBox, el);
    hoverBox.style.display = "block";
  }, true);

  // ---- right-click select ----
  document.addEventListener("contextmenu", function (e) {
    if (!state.active) return;
    e.preventDefault();
    e.stopPropagation();
    var el = elementAt(e);
    if (!el) return;
    selectElement(el);
  }, true);

  function elementAt(e) {
    var el = document.elementFromPoint(e.clientX, e.clientY);
    if (!el || el === document.body || el === document.documentElement) return null;
    if (isOurs(el)) return null;
    return el;
  }

  function isOurs(el) {
    if (el === toggleBtn || el === hoverBox || el === selBox || el === pinLayer) return true;
    if (pinLayer.contains(el)) return true;
    if (input && input.contains(el)) return true;
    if (popover && popover.contains(el)) return true;
    return false;
  }

  // ---- selection + NEW-comment composer ----
  function selectElement(el) {
    closeThread();
    state.selected = el;
    state.hover = null;
    hoverBox.style.display = "none";
    positionBox(selBox, el);
    selBox.style.display = "block";
    startLiveTracking();
    openComposer(el);
  }

  function clearSelection() {
    state.selected = null;
    selBox.style.display = "none";
    stopLiveTracking();
    if (input) { input.remove(); input = null; }
  }

  function openComposer(el) {
    if (input) input.remove();
    input = mkPanel(320);

    var summary = mkMeta(describe(el));
    var ta = mkTextarea("Describe the change… (Enter to send, Shift+Enter for newline)");

    var row = document.createElement("div");
    css(row, { display: "flex", gap: "6px", justifyContent: "flex-end", marginTop: "8px" });
    var cancel = mkBtn("Cancel", "transparent", THEME.muted, function () { clearSelection(); });
    cancel.style.border = "1px solid " + THEME.border;
    var send = mkBtn("Add comment", THEME.accent, THEME.accentFg, function () { submit(el, ta.value); });
    row.appendChild(cancel);
    row.appendChild(send);

    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(el, ta.value); }
    });

    input.appendChild(summary);
    input.appendChild(ta);
    input.appendChild(row);
    document.body.appendChild(input);
    positionFloat(input, el);
    ta.focus();
  }

  // ---- submit: hand a new comment up to the panel ----
  var _clientSeq = 0;
  function submit(el, comment) {
    comment = (comment || "").trim();
    if (!comment) return;
    var clientRef = "c" + Date.now() + "-" + (++_clientSeq);
    var payload = {
      type: "visual_edit_request",
      clientRef: clientRef,
      createdAt: new Date().toISOString(),
      selection: { mode: "single", elements: [buildElementPayload(el)] },
      comment: comment,
      previewUrl: location.href,
    };
    // Remember the live element so we can anchor the pin precisely once acked.
    _pendingCreate[clientRef] = { el: el, comment: comment };

    if (EMBEDDED) {
      postToHost({ source: "kiro-select-to-edit", type: "capture", clientRef: clientRef, payload: payload });
      // Show a provisional composer state while the panel creates the request.
      showComposerSending();
    } else if (CFG.backend) {
      fetch(CFG.backend.replace(/\/$/, "") + "/submit", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).catch(function (e) { console.warn("[select-to-edit] deliver failed", e); });
      clearSelection();
    } else {
      console.warn("[select-to-edit] no parent frame / no backend configured", payload);
      clearSelection();
    }
  }

  var _pendingCreate = Object.create(null);

  function showComposerSending() {
    if (!input) return;
    input.innerHTML = "";
    var m = mkMeta("Adding to request…");
    m.style.opacity = "0.9";
    input.appendChild(m);
  }

  // panel acked a created request → drop a real pin and open its thread
  function onCreated(d) {
    var pend = _pendingCreate[d.clientRef];
    var el = pend && pend.el;
    delete _pendingCreate[d.clientRef];
    clearSelection();
    if (!d.id) return;
    var item = {
      id: d.id, number: d.number, status: d.status || "sent",
      comment: (d.thread && d.thread[0] && d.thread[0].text) || (pend && pend.comment) || "",
      element: d.element || "", locator: d.locator || "", thread: d.thread || [],
    };
    upsertPin(item, el);
    openThread(d.id);
  }

  // ---- pin anchoring ----
  //
  // A pin used to be DELETED the moment its element stopped resolving. That is
  // exactly backwards for the two most interesting kinds of comment:
  //
  //   "delete this"  → the agent removes the node, and the bubble reporting the
  //                    work disappears with it
  //   "add a X here" → there is no element yet, so there is nothing to anchor to
  //
  // So resolution is a chain, best first, and it never fails. `kind` is returned
  // so the pin can show HOW firmly it is attached.
  //
  //   exact   [data-kiro-cid="<cid>"] — the agent stamped the element it created
  //           or changed for this comment. Checked FIRST so a re-homed pin wins
  //           over a stale locator that still matches something else.
  //   locator the selector captured at comment time
  //   parent  the element's former parent — for a node that has been removed
  //   point   where the user clicked, in document space
  //   page    bottom-left of the page, when even the point is unusable
  function resolveAnchor(it, existing) {
    var el = null;
    // The agent's explicit hand-off always wins, even over a live cached element,
    // so a pin re-homes onto a newly created node as soon as it appears.
    try { el = document.querySelector('[data-kiro-cid="' + cssEscape(it.id) + '"]'); } catch (_) { el = null; }
    if (el) return { el: el, kind: "exact" };
    if (existing && existing.el && existing.el.isConnected && existing.kind === "locator") {
      return { el: existing.el, kind: "locator" };
    }
    if (it.locator) {
      try { el = document.querySelector(it.locator); } catch (_) { el = null; }
      if (el) return { el: el, kind: "locator" };
    }
    if (it.parentLocator) {
      try { el = document.querySelector(it.parentLocator); } catch (_) { el = null; }
      if (el) return { el: el, kind: "parent" };
    }
    if (it.point && typeof it.point.x === "number") return { el: null, kind: "point" };
    return { el: null, kind: "page" };
  }

  // CSS.escape is absent in older engines and the ids are hex-ish anyway.
  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(String(s));
    return String(s).replace(/["\\]/g, "\\$&");
  }

  // ---- pins ----
  function reconcile(items) {
    var seen = Object.create(null);
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (!it || !it.id) continue;
      seen[it.id] = true;
      var a = resolveAnchor(it, pins[it.id]);
      upsertPin(it, a.el, a.kind);
    }
    for (var id in pins) if (!seen[id]) removePin(id);
    if (popover && popoverId && pins[popoverId]) maybeRenderThread(pins[popoverId].item);
    repositionPins();
  }

  function upsertPin(item, el, kind) {
    var p = pins[item.id];
    if (!p) {
      var dot = document.createElement("button");
      css(dot, {
        position: "fixed", zIndex: 2147483645, width: "24px", height: "24px",
        borderRadius: "50% 50% 50% 2px", border: "2px solid #fff", cursor: "pointer",
        font: "700 12px system-ui, sans-serif", color: "#fff", display: "flex",
        alignItems: "center", justifyContent: "center", padding: 0,
        boxShadow: "0 2px 8px rgba(0,0,0,.4)", transition: "transform .1s",
      });
      dot.addEventListener("click", function (ev) {
        ev.stopPropagation();
        openThread(item.id);
      });
      dot.addEventListener("mouseenter", function () { dot.style.transform = "scale(1.12)"; });
      dot.addEventListener("mouseleave", function () { dot.style.transform = "scale(1)"; });
      pinLayer.appendChild(dot);
      p = pins[item.id] = { id: item.id, item: item, el: el, kind: kind, dot: dot };
    }
    p.item = item;
    p.el = el;
    p.kind = kind || "locator";
    p.dot.textContent = String(item.number || "•");
    p.dot.style.background = statusColor(item.status);
    p.dot.style.color = readableOn(statusColor(item.status));
    // A pin that is not on its own element says so: dashed border, and the tooltip
    // explains WHY, so a floating bubble never looks like a mispositioned one.
    var loose = p.kind !== "exact" && p.kind !== "locator";
    p.dot.style.borderStyle = loose ? "dashed" : "solid";
    p.dot.style.opacity = loose ? "0.9" : "1";
    p.dot.title = "#" + (item.number || "") + " — " + (item.comment || "") +
      (p.kind === "parent" ? "\n(element removed — pinned to its parent)"
       : p.kind === "point" ? "\n(waiting for the new element — pinned where you commented)"
       : p.kind === "page" ? "\n(not on this page)" : "");
    positionPin(p);
  }

  function removePin(id) {
    var p = pins[id];
    if (!p) return;
    if (p.dot && p.dot.parentNode) p.dot.parentNode.removeChild(p.dot);
    delete pins[id];
    if (popoverId === id) closeThread();
  }

  function statusColor(s) {
    if (s === "done") return THEME.ok;
    if (s === "sent") return THEME.accent;
    return THEME.warn; // new
  }

  // Pick black/white for the number on top of a status fill, by the fill's
  // luminance — keeps the digit readable on any theme's warn/accent/ok color.
  function parseHex(h) {
    if (typeof h !== "string") return null;
    h = h.trim().replace(/^#/, "");
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    if (h.length !== 6) return null;
    var n = parseInt(h, 16);
    if (isNaN(n)) return null;
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }
  function readableOn(bg) {
    var c = parseHex(bg);
    if (!c) return "#ffffff";
    var yiq = (c.r * 299 + c.g * 587 + c.b * 114) / 1000;
    return yiq >= 150 ? "#0b0b0b" : "#ffffff";
  }

  // A pin with no element of its own. Two placements:
  //   point → the document position the user clicked, converted back to viewport
  //           space so it tracks scrolling like every other pin
  //   page  → bottom-left of the page, the documented home for a comment that
  //           cannot be placed at all
  // Both are clamped into view: an anchor recorded far down a page that has since
  // got shorter would otherwise put the bubble somewhere unreachable.
  function positionLoosePin(p) {
    var pt = p.item && p.item.point;
    p.dot.style.display = "flex";
    var left, top;
    if (p.kind === "point" && pt && typeof pt.x === "number") {
      left = pt.x - window.scrollX;
      top = pt.y - window.scrollY;
    } else {
      left = PAGE_PIN_INSET;
      top = window.innerHeight - PAGE_PIN_INSET - 24;
    }
    var maxL = Math.max(0, window.innerWidth - 28);
    var maxT = Math.max(0, window.innerHeight - 28);
    p.dot.style.left = Math.round(Math.min(Math.max(left, 4), maxL)) + "px";
    p.dot.style.top = Math.round(Math.min(Math.max(top, 4), maxT)) + "px";
  }

  function positionPin(p) {
    // No element: place it from the stored anchor rather than hiding it. Hiding was
    // the old behaviour and it is what made "delete this" comments disappear.
    if (!p.el || !p.el.isConnected) return positionLoosePin(p);
    var r = p.el.getBoundingClientRect();
    // A zero-size box is a real element that renders nothing (display:contents, an
    // emptied container) — fall back rather than hide.
    if (r.width === 0 && r.height === 0) return positionLoosePin(p);
    p.dot.style.display = "flex";
    p.dot.style.left = Math.round(r.left - 12) + "px";
    p.dot.style.top = Math.round(r.top - 12) + "px";
  }

  function repositionPins() {
    for (var id in pins) positionPin(pins[id]);
    if (popover && popoverId && pins[popoverId]) positionFloat(popover, pins[popoverId].el || pins[popoverId].dot);
  }

  function scrollPinIntoView(p) {
    if (p && p.el && p.el.scrollIntoView) {
      try { p.el.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (_) {}
    }
  }

  window.addEventListener("scroll", repositionPins, true);
  window.addEventListener("resize", repositionPins, true);
  setInterval(repositionPins, 600); // catch reflow/layout shifts (hot-reload, images)

  // ---- thread popover ----
  function openThread(id) {
    var p = pins[id];
    if (!p) return;
    if (input) clearSelection();
    if (popover) popover.remove();
    popoverId = id;
    popover = mkPanel(340);
    renderThreadBody(p.item);
    document.body.appendChild(popover);
    positionFloat(popover, p.el || p.dot);
  }

  function closeThread() {
    if (popover) popover.remove();
    popover = null;
    popoverId = null;
    popoverSig = null;
  }

  function threadSig(item) {
    return (item.status || "") + "|" + ((item.thread && item.thread.length) || 0);
  }

  // Only redraw the open popover when its content actually changed, so a 5s
  // poll never wipes a follow-up the user is mid-typing.
  function maybeRenderThread(item) {
    if (threadSig(item) === popoverSig) return;
    renderThreadBody(item);
  }

  function renderThreadBody(item) {
    if (!popover) return;
    // Preserve any in-progress follow-up text across a redraw.
    var prevText = "";
    var oldTa = popover.querySelector("textarea");
    if (oldTa) prevText = oldTa.value;
    popover.innerHTML = "";

    // header
    var head = document.createElement("div");
    css(head, { display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" });
    var badge = document.createElement("span");
    badge.textContent = "#" + (item.number || "");
    css(badge, {
      background: statusColor(item.status), color: readableOn(statusColor(item.status)), borderRadius: "6px",
      font: "700 11px system-ui", padding: "1px 7px",
    });
    var title = document.createElement("div");
    title.textContent = item.element || describeStatus(item.status);
    css(title, { flex: 1, font: "600 12px system-ui", color: THEME.textStrong, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" });
    var chip = mkStatusChip(item.status);
    var close = mkBtn("✕", "transparent", THEME.muted, closeThread);
    css(close, { padding: "2px 6px", fontSize: "12px", border: "none" });
    head.appendChild(badge); head.appendChild(title); head.appendChild(chip); head.appendChild(close);
    popover.appendChild(head);

    // messages
    var list = document.createElement("div");
    css(list, { maxHeight: "220px", overflowY: "auto", display: "flex", flexDirection: "column", gap: "6px", padding: "2px 0" });
    var thread = (item.thread && item.thread.length) ? item.thread : [{ role: "user", text: item.comment }];
    for (var i = 0; i < thread.length; i++) list.appendChild(mkBubble(thread[i]));
    popover.appendChild(list);
    list.scrollTop = list.scrollHeight;

    // composer (follow-up)
    var ta = mkTextarea("Reply / add a follow-up…");
    ta.style.minHeight = "38px";
    ta.style.marginTop = "8px";
    ta.value = prevText;
    var row = document.createElement("div");
    css(row, { display: "flex", gap: "6px", justifyContent: "flex-end", marginTop: "6px" });
    // A reply on an existing comment becomes a NEW comment in the CURRENT draft,
    // linked back to this one via followUpTo — the sent request is never mutated.
    var send = mkBtn("Follow up →", THEME.accent, THEME.accentFg, function () {
      var t = (ta.value || "").trim();
      if (!t) return;
      // optimistic bubble
      list.appendChild(mkBubble({ role: "user", text: t }));
      list.scrollTop = list.scrollHeight;
      ta.value = "";
      if (EMBEDDED) postToHost({ source: "kiro-select-to-edit", type: "dispatch", id: item.id, text: t });
    });
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send.click(); }
    });
    row.appendChild(send);
    popover.appendChild(ta);
    popover.appendChild(row);
    popoverSig = threadSig(item);
  }

  function describeStatus(s) {
    if (s === "done") return "Done";
    if (s === "sent") return "In progress";
    return "New request";
  }

  function mkStatusChip(s) {
    var c = document.createElement("span");
    c.textContent = describeStatus(s);
    css(c, {
      font: "600 10px system-ui", padding: "2px 7px", borderRadius: "999px",
      color: statusColor(s), border: "1px solid " + statusColor(s), whiteSpace: "nowrap",
    });
    return c;
  }

  function mkBubble(msg) {
    var role = msg.role || "agent";
    var wrap = document.createElement("div");
    css(wrap, { display: "flex", flexDirection: "column", alignItems: role === "user" ? "flex-end" : "flex-start", gap: "2px" });
    var isUser = role === "user";
    var isSystem = role === "system";

    var label = document.createElement("div");
    label.textContent = isUser ? "You" : isSystem ? "" : "Agent";
    css(label, { font: "600 10px system-ui", color: THEME.muted, padding: "0 4px" });

    var b = document.createElement("div");
    b.textContent = msg.text || "";
    css(b, {
      maxWidth: "88%", font: "500 12px/1.45 system-ui", padding: "7px 10px", borderRadius: "10px",
      whiteSpace: "pre-wrap", wordBreak: "break-word",
      // Card surface (distinct from the elevated popover) + strongest text token for contrast.
      background: isSystem ? "transparent" : THEME.card,
      color: isSystem ? THEME.muted : THEME.textStrong,
      border: "1px solid " + (isUser ? THEME.accent : THEME.border),
      borderLeft: isUser ? "1px solid " + THEME.accent : "3px solid " + (isSystem ? THEME.border : THEME.accent),
      fontStyle: isSystem ? "italic" : "normal",
    });
    if (label.textContent) wrap.appendChild(label);
    wrap.appendChild(b);
    return wrap;
  }

  // ---- live tracking of the highlight while a selection is open ----
  var rafId = null;
  function startLiveTracking() {
    stopLiveTracking();
    var tick = function () {
      if (!state.selected) return;
      if (!state.selected.isConnected) { clearSelection(); return; }
      positionBox(selBox, state.selected);
      if (input) positionFloat(input, state.selected);
      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);
  }
  function stopLiveTracking() {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = null;
  }

  // ---- payload assembly ----
  function buildElementPayload(el) {
    var r = el.getBoundingClientRect();
    var cs = getComputedStyle(el);
    var relevant = {};
    ["display", "position", "top", "right", "bottom", "left",
     "margin", "padding", "gap", "flexDirection", "justifyContent",
     "alignItems", "gridTemplateColumns", "width", "height",
     "fontSize", "color", "backgroundColor", "borderRadius"].forEach(function (k) {
      var v = cs[k];
      if (v && v !== "normal" && v !== "auto" && v !== "none") relevant[k] = v;
    });
    var source = resolveSource(el);
    var html = el.outerHTML || "";
    if (html.length > SNIPPET_MAX) html = html.slice(0, SNIPPET_MAX) + "…";
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || "",
      classes: Array.prototype.slice.call(el.classList),
      locator: cssPath(el),
      // Two extra anchors so a pin can OUTLIVE its element. A comment asking to
      // delete this node, or to add one that does not exist yet, must not make its
      // own bubble vanish — that is the moment the comment matters most.
      //   parentLocator → where the element used to live
      //   point         → document-space coords of the click, the last resort
      parentLocator: el.parentElement ? cssPath(el.parentElement) : "",
      point: { x: Math.round(r.x + window.scrollX), y: Math.round(r.y + window.scrollY) },
      boundingRect: { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) },
      source: source,
      htmlSnippet: html,
      relevantStyles: relevant,
    };
  }

  // A best-effort unique-ish CSS path so pins re-anchor after a preview reload.
  function cssPath(el) {
    if (el.id) { try { return "#" + CSS.escape(el.id); } catch (_) { return "#" + el.id; } }
    var parts = [];
    var node = el;
    while (node && node.nodeType === 1 && node !== document.body && node !== document.documentElement) {
      var tag = node.tagName.toLowerCase();
      if (node.id) { try { parts.unshift("#" + CSS.escape(node.id)); } catch (_) { parts.unshift("#" + node.id); } break; }
      var parent = node.parentNode;
      if (parent && parent.children) {
        var idx = 0, n = 0;
        for (var i = 0; i < parent.children.length; i++) {
          if (parent.children[i].tagName === node.tagName) { n++; if (parent.children[i] === node) idx = n; }
        }
        if (n > 1) tag += ":nth-of-type(" + idx + ")";
      }
      parts.unshift(tag);
      node = parent;
    }
    return parts.join(" > ");
  }

  function resolveSource(el) {
    var ds = el.getAttribute && el.getAttribute("data-kiro-source");
    if (ds) {
      var m = /^(.*):(\d+):(\d+)$/.exec(ds);
      if (m) return { file: m[1], line: +m[2], column: +m[3], confidence: "high" };
    }
    try {
      var key = Object.keys(el).find(function (k) {
        return k.indexOf("__reactFiber") === 0 || k.indexOf("__reactInternalInstance") === 0;
      });
      if (key) {
        var fiber = el[key];
        var dbg = fiber && (fiber._debugSource || (fiber._debugOwner && fiber._debugOwner._debugSource));
        if (dbg && dbg.fileName) return { file: dbg.fileName, line: dbg.lineNumber || 0, column: dbg.columnNumber || 0, confidence: "medium" };
      }
    } catch (_) {}
    return { file: "", line: 0, column: 0, confidence: "low" };
  }

  // ---- floating panel positioning ----
  function positionFloat(node, el) {
    if (!node || !el) return;
    var r = el.getBoundingClientRect();
    var w = node.offsetWidth || 320, h = node.offsetHeight || 120, gap = 10;
    var left = Math.min(r.left, window.innerWidth - w - 8);
    var top = r.bottom + gap;
    if (top + h > window.innerHeight) top = Math.max(8, r.top - h - gap);
    node.style.left = Math.max(8, left) + "px";
    node.style.top = top + "px";
  }

  // ---- element factories ----
  function mkPanel(width) {
    var el = document.createElement("div");
    css(el, {
      position: "fixed", zIndex: 2147483647, width: width + "px",
      background: THEME.bgElevated, border: "1px solid " + THEME.border, borderRadius: "12px",
      padding: "12px", boxShadow: "0 12px 40px rgba(0,0,0,.55)",
      font: "13px system-ui, sans-serif", color: THEME.textStrong,
    });
    return el;
  }
  function mkMeta(text) {
    var d = document.createElement("div");
    d.textContent = text;
    css(d, { fontSize: "11px", color: THEME.muted, marginBottom: "6px" });
    return d;
  }
  function mkTextarea(ph) {
    var ta = document.createElement("textarea");
    ta.placeholder = ph;
    css(ta, {
      width: "100%", minHeight: "48px", resize: "vertical", boxSizing: "border-box",
      background: THEME.panel, color: THEME.textStrong, border: "1px solid " + THEME.border,
      borderRadius: "8px", padding: "8px", font: "13px system-ui, sans-serif",
    });
    return ta;
  }
  function describe(el) {
    var t = el.tagName.toLowerCase();
    if (el.id) return t + "#" + el.id;
    if (el.classList.length) return t + "." + Array.prototype.slice.call(el.classList).slice(0, 2).join(".");
    return t + " element selected";
  }
  function positionBox(box, el) {
    var r = el.getBoundingClientRect();
    css(box, { left: r.left + "px", top: r.top + "px", width: r.width + "px", height: r.height + "px" });
  }
  function mkBox(border, fill) {
    var b = document.createElement("div");
    css(b, {
      position: "fixed", zIndex: 2147483643, pointerEvents: "none",
      border: "2px solid " + border, background: fill, borderRadius: "3px", transition: "none",
    });
    return b;
  }
  function mkBtn(label, bg, fg, onClick) {
    var b = document.createElement("button");
    b.textContent = label;
    css(b, {
      padding: "6px 10px", borderRadius: "8px", border: "none", cursor: "pointer",
      background: bg, color: fg || "#fff", font: "600 12px system-ui, sans-serif",
    });
    b.addEventListener("click", onClick);
    return b;
  }
  function css(el, styles) { for (var k in styles) el.style[k] = styles[k]; }
})();
