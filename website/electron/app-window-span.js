"use strict";

/**
 * app-window-span.js: one avatar shared across every display, handed off by drag.
 *
 * An overlay an app declares with `displays: "all"` gets one copy per display,
 * each covering that display's bounds, but the app's avatar lives on exactly ONE
 * of them at a time: the active copy. Every other copy is told it is inactive and
 * stays click-through, so a multi-monitor user sees one avatar, not one per
 * screen. The avatar moves to another display only when the user drags it across
 * the boundary; it never follows the cursor at rest.
 *
 * The shell owns the hand-off because pointer events stop at a window edge and
 * only `screen.getCursorScreenPoint()` follows a drag between displays. While a
 * drag is in flight every copy takes the pointer, so whichever display the button
 * is released over can end it.
 *
 * The active display is remembered per app window across restarts, in a 0600
 * file in the shell's own data directory (the shell cannot resolve the gateway's).
 *
 * Messages addressed to a spanning window go to the active copy, held in order
 * until it is ready; the queue follows the avatar across a hand-off.
 *
 * Driven by app-window-host.js, which owns the windows; this module owns only
 * which copy is active and the drag between them. Pure of Electron imports: the
 * host passes `screen` and every window operation in.
 */

const fs = require("fs");

/** ~60fps, the cursor cadence the overlay hit-region poll uses. */
const DRAG_POLL_MS = 16;
/** Force-stop a drag that never saw its release (a lost mouseup, a blurred window). */
const DRAG_SAFETY_MS = 10_000;
/** Messages held for an active copy that is not ready yet. */
const MAX_SPAN_INBOX = 32;
/** The largest avatar box a page may drag, in page pixels. */
const MAX_DRAG_BOX = 4_096;

const SPAN_CHANNELS = Object.freeze({
  active: "app-window:active",
  drag: "app-window:drag",
});

/** The display whose bounds hold the point, or null for a point in a gap. */
function displayAtPoint(displays, x, y) {
  return displays.find((d) => (
    x >= d.bounds.x && x < d.bounds.x + d.bounds.width
    && y >= d.bounds.y && y < d.bounds.y + d.bounds.height
  )) || null;
}

/** The display nearest the point by squared edge distance. */
function nearestDisplay(displays, x, y) {
  let best = displays[0] || null;
  let bestDist = Infinity;
  for (const d of displays) {
    const dx = Math.max(d.bounds.x - x, 0, x - (d.bounds.x + d.bounds.width));
    const dy = Math.max(d.bounds.y - y, 0, y - (d.bounds.y + d.bounds.height));
    const dist = dx * dx + dy * dy;
    if (dist < bestDist) {
      bestDist = dist;
      best = d;
    }
  }
  return best;
}

/**
 * A dragged box's top-left, kept on its display: it may hang half off the left
 * or right edge, never off the top or bottom. Every value is in page pixels.
 */
function clampBox(x, y, box, area) {
  return {
    x: Math.max(-box.width / 2, Math.min(area.width - box.width / 2, x)),
    y: Math.max(0, Math.min(area.height - box.height, y)),
  };
}

/** A drag box from a page, or null: two offsets and a size, all finite and bounded. */
function sanitizeDragBox(raw) {
  if (!raw || typeof raw !== "object") return null;
  const { offsetX, offsetY, width, height } = raw;
  const nums = [offsetX, offsetY, width, height];
  if (!nums.every((n) => typeof n === "number" && Number.isFinite(n))) return null;
  if (width <= 0 || height <= 0 || nums.some((n) => Math.abs(n) > MAX_DRAG_BOX)) return null;
  return { offsetX, offsetY, width, height };
}

/** The remembered active display per spanning window, `{"<app>/<id>": displayId}`. */
function createDisplayMemory(file) {
  let cache = null;
  function load() {
    if (cache) return cache;
    cache = {};
    if (!file) return cache;
    try {
      const parsed = JSON.parse(fs.readFileSync(file, "utf-8"));
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        for (const [key, value] of Object.entries(parsed)) {
          if (typeof value === "number" && Number.isFinite(value)) cache[key] = value;
        }
      }
    } catch {
      /* first run or unreadable: elect from the cursor */
    }
    return cache;
  }
  return {
    get: (key) => (Object.prototype.hasOwnProperty.call(load(), key) ? load()[key] : null),
    set(key, displayId) {
      const all = load();
      if (all[key] === displayId) return;
      all[key] = displayId;
      if (!file) return;
      try {
        fs.writeFileSync(file, JSON.stringify(all), { mode: 0o600 });
      } catch {
        /* a failed write must never break the window */
      }
    },
  };
}

/**
 * @param {{
 *   screen: object,
 *   memory: {get: (key: string) => number|null, set: (key: string, id: number) => void},
 *   copies: (key: string) => object[],
 *   createCopy: (app: string, decl: object, display: object) => object|null,
 *   destroyCopy: (rec: object) => void,
 *   send: (rec: object, channel: string, ...args: unknown[]) => void,
 *   setIgnore: (rec: object, ignore: boolean) => void,
 *   setTimer?: typeof setInterval, clearTimer?: typeof clearInterval,
 *   setTimeoutFn?: typeof setTimeout, clearTimeoutFn?: typeof clearTimeout,
 * }} ctx
 */
function createSpans(ctx) {
  const { screen, memory } = ctx;
  const setTimer = ctx.setTimer || setInterval;
  const clearTimer = ctx.clearTimer || clearInterval;
  const setTimeoutFn = ctx.setTimeoutFn || setTimeout;
  const clearTimeoutFn = ctx.clearTimeoutFn || clearTimeout;
  /** "<app>/<id>" -> {app, id, active, inbox, drag, settled} */
  const spans = new Map();

  function allDisplays() {
    try {
      return screen.getAllDisplays();
    } catch {
      return [];
    }
  }

  function cursorPoint() {
    try {
      return screen.getCursorScreenPoint();
    } catch {
      return null;
    }
  }

  function copyOn(span, displayId) {
    return ctx.copies(spanKey(span.app, span.id)).find((r) => r.displayId === displayId) || null;
  }

  function activeCopy(span) {
    return span.active === null ? null : copyOn(span, span.active);
  }

  /** A screen point relative to `bounds`, in the active copy's page pixels. */
  function toPage(span, point, bounds) {
    const active = activeCopy(span);
    const zoom = active ? active.win.webContents.getZoomFactor() || 1 : 1;
    return {
      x: (point.x - bounds.x) / zoom,
      y: (point.y - bounds.y) / zoom,
      width: bounds.width / zoom,
      height: bounds.height / zoom,
    };
  }

  function stateOf(key) {
    return spans.get(key) || null;
  }

  function ensure(app, id) {
    const key = spanKey(app, id);
    let span = spans.get(key);
    if (!span) {
      span = { app, id, active: null, inbox: [], drag: null, settled: null };
      spans.set(key, span);
    }
    return span;
  }

  /** Elect the active display: the remembered one, else the cursor's, else the primary. */
  function elect(span, displays) {
    if (span.active !== null && displays.some((d) => d.id === span.active)) return;
    const remembered = memory.get(spanKey(span.app, span.id));
    if (remembered !== null && displays.some((d) => d.id === remembered)) {
      span.active = remembered;
      return;
    }
    let primary = null;
    try {
      primary = screen.getPrimaryDisplay();
    } catch {
      primary = displays[0] || null;
    }
    const cursor = cursorPoint();
    const atCursor = cursor ? displayAtPoint(displays, cursor.x, cursor.y) : null;
    const chosen = atCursor || primary || displays[0] || null;
    span.active = chosen ? chosen.id : null;
  }

  /**
   * Open a copy on every display that has none. Idempotent per display, and an
   * already valid active display is kept, so a hot-plugged monitor never moves
   * the avatar. Returns the active copy, or any copy, or null.
   */
  function open(app, decl) {
    const span = ensure(app, decl.id);
    const displays = allDisplays();
    elect(span, displays);
    for (const display of displays) {
      if (copyOn(span, display.id)) continue;
      const rec = ctx.createCopy(app, decl, display);
      // A copy plugged in mid-drag takes the pointer like the others, so the
      // release can land on it.
      if (rec && span.drag) ctx.setIgnore(rec, false);
    }
    return activeCopy(span) || ctx.copies(spanKey(app, decl.id))[0] || null;
  }

  /** Tell one copy whether it hosts the avatar, with the live drag point mid-drag. */
  function sendActiveState(span, rec) {
    if (!rec || !rec.ready) return;
    if (rec.displayId !== span.active) {
      ctx.send(rec, SPAN_CHANNELS.active, { active: false });
      return;
    }
    let state = { active: true };
    // A drag released before this copy's page loaded: the avatar rests where it was dropped.
    const settled = span.settled && span.settled.displayId === span.active ? span.settled : null;
    span.settled = null;
    if (settled) state = { active: true, x: settled.x, y: settled.y };
    if (span.drag) {
      // A copy that missed the hand-off must adopt the drag in flight, not read
      // a bare activation as "at rest".
      const cursor = cursorPoint();
      const display = allDisplays().find((d) => d.id === span.active);
      if (cursor && display) {
        const page = toPage(span, cursor, display.bounds);
        state = { active: true, x: page.x - span.drag.offsetX, y: page.y - span.drag.offsetY, dragging: true };
      }
    }
    ctx.send(rec, SPAN_CHANNELS.active, state);
    flushInbox(span);
  }

  function flushInbox(span) {
    const active = activeCopy(span);
    if (!active || !active.ready || span.inbox.length === 0) return;
    for (const args of span.inbox.splice(0)) ctx.send(active, ...args);
  }

  /** A copy's page said ready: tell it its role. */
  function copyReady(rec) {
    const span = stateOf(rec.span);
    if (!span) return;
    // A copy that loads mid-drag listens for the release like the others.
    if (span.drag) ctx.send(rec, SPAN_CHANNELS.drag, "start");
    sendActiveState(span, rec);
  }

  /**
   * Deliver to the copy hosting the avatar, or hold it, in order, until that copy
   * is ready. Returns false when the message was dropped (the inbox is full).
   */
  function deliver(app, id, channel, ...args) {
    const span = ensure(app, id);
    const active = activeCopy(span);
    if (active && active.ready) {
      ctx.send(active, channel, ...args);
      return true;
    }
    if (span.inbox.length >= MAX_SPAN_INBOX) return false;
    span.inbox.push([channel, ...args]);
    return true;
  }

  /** Move the avatar to another display's copy, at a point in that copy's page pixels. */
  function transfer(span, displayId, x, y, dragging) {
    const next = copyOn(span, displayId);
    if (!next) return;
    const previous = activeCopy(span);
    if (previous && previous !== next) {
      ctx.send(previous, SPAN_CHANNELS.active, { active: false });
      if (!dragging) ctx.setIgnore(previous, true);
    }
    span.active = displayId;
    // The new copy's old regions describe nothing on screen; it reports fresh ones.
    next.regions = [];
    if (next.ready) {
      ctx.send(next, SPAN_CHANNELS.active, dragging ? { active: true, x, y, dragging: true } : { active: true, x, y });
      flushInbox(span);
    }
  }

  function dragTick(span) {
    const cursor = cursorPoint();
    if (!cursor || !span.drag) return;
    const displays = allDisplays();
    const target = displayAtPoint(displays, cursor.x, cursor.y) || nearestDisplay(displays, cursor.x, cursor.y);
    if (!target) return;
    // Crossing into a display with a copy hands the avatar over at the crossing
    // point. A display with no copy yet (hot-plugged mid-drag) keeps the avatar
    // where it is, clamped to its own display's edge.
    if (target.id !== span.active && copyOn(span, target.id)) {
      const page = toPage(span, cursor, target.bounds);
      transfer(span, target.id, page.x - span.drag.offsetX, page.y - span.drag.offsetY, true);
      memory.set(spanKey(span.app, span.id), target.id);
      return;
    }
    const activeDisplay = displays.find((d) => d.id === span.active) || target;
    const page = toPage(span, cursor, activeDisplay.bounds);
    const point = clampBox(page.x - span.drag.offsetX, page.y - span.drag.offsetY, span.drag, page);
    const active = activeCopy(span);
    if (active && active.ready) ctx.send(active, SPAN_CHANNELS.drag, "move", point.x, point.y);
  }

  /** The active copy began dragging the avatar box `raw` (offsets from the cursor). */
  function beginDrag(rec, raw) {
    const span = stateOf(rec.span);
    const box = sanitizeDragBox(raw);
    if (!span || !box || rec.displayId !== span.active) return;
    // A drag that never saw its release settles first, as its release would have.
    stopDrag(span, true);
    span.settled = null;
    span.drag = { ...box, poll: null, safety: null };
    for (const copy of ctx.copies(rec.span)) {
      ctx.setIgnore(copy, false);
      if (copy.ready) ctx.send(copy, SPAN_CHANNELS.drag, "start");
    }
    span.drag.safety = setTimeoutFn(() => stopDrag(span, true), DRAG_SAFETY_MS);
    span.drag.safety?.unref?.();
    span.drag.poll = setTimer(() => dragTick(span), DRAG_POLL_MS);
    span.drag.poll?.unref?.();
  }

  /** Any copy saw the release: end the drag wherever the cursor now is. */
  function endDrag(rec) {
    const span = stateOf(rec.span);
    if (span) stopDrag(span, true);
  }

  function stopDrag(span, settle) {
    if (!span.drag) return;
    // One last hand-off check, so an avatar released the instant it crossed does
    // not snap back to the display the previous tick saw.
    if (settle) {
      try {
        dragTick(span);
      } catch {
        /* windows torn down mid-drag */
      }
    }
    const drag = span.drag;
    clearTimer(drag.poll);
    clearTimeoutFn(drag.safety);
    span.drag = null;
    const cursor = settle ? cursorPoint() : null;
    const display = cursor ? allDisplays().find((d) => d.id === span.active) : null;
    const active = activeCopy(span);
    if (cursor && display && active) {
      const page = toPage(span, cursor, display.bounds);
      const point = clampBox(page.x - drag.offsetX, page.y - drag.offsetY, drag, page);
      // A copy still loading hears where the avatar settled in its activation.
      if (active.ready) ctx.send(active, SPAN_CHANNELS.drag, "end", point.x, point.y);
      else span.settled = { displayId: span.active, x: point.x, y: point.y };
      memory.set(spanKey(span.app, span.id), span.active);
    }
    // Every copy goes back to click-through; the hit-region poll makes the active
    // one interactive again over the regions its page reports.
    for (const copy of ctx.copies(spanKey(span.app, span.id))) ctx.setIgnore(copy, true);
  }

  /** A copy is gone: if it hosted the avatar, a surviving copy takes it over. */
  function copyClosed(rec) {
    const span = stateOf(rec.span);
    if (!span || rec.displayId !== span.active) return;
    const survivor = ctx.copies(rec.span).find((r) => r !== rec) || null;
    span.active = survivor ? survivor.displayId : null;
    if (survivor) sendActiveState(span, survivor);
    if (!survivor) stopDrag(span, false);
  }

  /** The display topology changed: copies follow it, and the avatar stays visible. */
  function displaysChanged(decls) {
    const displays = allDisplays();
    for (const span of [...spans.values()]) {
      const key = spanKey(span.app, span.id);
      if (ctx.copies(key).length === 0) continue;
      // Copies for the displays that exist now open first, and the avatar is
      // re-elected among them, so removing the display every copy was on (an
      // undock, a monitor swap) never leaves the window with no copy at all.
      const before = span.active;
      const decl = decls(span.app, span.id);
      if (decl) open(span.app, decl);
      for (const rec of ctx.copies(key)) {
        const display = displays.find((d) => d.id === rec.displayId);
        if (!display) ctx.destroyCopy(rec);
        else rec.win.setBounds(display.bounds);
      }
      const copies = ctx.copies(key);
      if (copies.length > 0 && !copies.some((r) => r.displayId === span.active)) {
        span.active = copies[0].displayId;
      }
      // The copy that took the avatar over is told so; one still loading hears
      // it when its page is ready.
      const active = activeCopy(span);
      if (span.active !== before && active) sendActiveState(span, active);
    }
  }

  function isDragging(key) {
    return !!stateOf(key)?.drag;
  }

  function isActive(rec) {
    const span = stateOf(rec.span);
    return !!span && rec.displayId === span.active;
  }

  /** The whole spanning window closed: drop its drag and queue, keep the memory. */
  function closed(key) {
    const span = stateOf(key);
    if (!span) return;
    stopDrag(span, false);
    spans.delete(key);
  }

  function closeAll() {
    for (const key of [...spans.keys()]) closed(key);
  }

  return {
    open,
    copyReady,
    deliver,
    beginDrag,
    endDrag,
    copyClosed,
    displaysChanged,
    isDragging,
    isActive,
    closed,
    closeAll,
  };
}

function spanKey(app, id) {
  return `${app}/${id}`;
}

module.exports = {
  DRAG_POLL_MS,
  DRAG_SAFETY_MS,
  MAX_SPAN_INBOX,
  SPAN_CHANNELS,
  clampBox,
  createDisplayMemory,
  createSpans,
  displayAtPoint,
  nearestDisplay,
  sanitizeDragBox,
  spanKey,
};
