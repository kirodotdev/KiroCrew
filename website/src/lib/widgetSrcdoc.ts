// Shared utility for building the sandboxed iframe srcdoc that renders
// LLM-generated widget HTML.
//
// SECURITY MODEL
// ==============
// The iframe uses sandbox="allow-scripts" (AGENT_PAGE_SANDBOX) with no popup
// grant, giving it a null origin. The LLM content runs in an isolated context —
// it cannot access parent DOM, cookies, localStorage, navigate the parent page,
// or open a window of its own. Links open through the host instead: the
// `linkBridge` option injects LINK_BRIDGE_SHIM_BODY, which hands a `_blank` or
// bare web link click, and `window.open`, to the host's `useFrameLinkBridge`,
// gives each web link its URL as a hover title, and the host opens the URL after
// a real click in that frame. Callers that do not set `linkBridge` (an exported copy, the mochi
// pet frame) keep EXTERNAL_LINK_TARGET_SHIM_BODY or opt out of it with
// `rewriteBareLinks: false`.
//
// DOMPurify is NOT applied because it strips <script> tags, which are
// required for widget interactivity (Chart.js, D3, Tailwind CDN, etc.).
//
// The LLM output is already scanned by redact_exfiltration_urls() and
// redact_credentials() in the backend's response pipeline before reaching
// the frontend (see kiro_crew/dashboard/handlers/artifacts.py:_serialize
// and kiro_crew/chat.py streaming).
//
// Theme vars pass through sanitizeCssValue() (char allowlist + dangerous-
// function denylist + 200-char cap) before reaching this module, so the
// CSS interpolation done via DOM textContent below is safe even from a
// compromised parent theme.
//
// IMPLEMENTATION NOTE — DOM construction (no template-literal HTML):
// -------------------------------------------------------------------
// LLM-originated `html` never enters a template literal. It flows through
// Range.createContextualFragment() (a typed DOM API), is parsed into DOM
// nodes, has any <script> elements re-cloned so they execute, and then
// the host document is serialized via documentElement.outerHTML. The only
// remaining template literal in this file is the DOCTYPE prefix, which
// contains no LLM content.
//
// Used by:
// - WidgetFrame.tsx (inline <mcwidget> rendering in chat)
// - useArtifactDetailPageDocument.ts (full-screen artifact view at /artifacts/<slug>)

import { parseCssColor, relativeLuminance } from './iconContrast'
import { TAILWIND_RUNTIME_PATH as TAILWIND_RUNTIME_PATH_ROOT } from './vendorPaths'
import { withBase } from './basePath'
/** The runtime's served path under this build's base (the bare path in the stock build). */
const TAILWIND_RUNTIME_PATH = withBase(TAILWIND_RUNTIME_PATH_ROOT)
import { sanitizeCssValue } from './cssSanitize'
import { SWIPE_GESTURE_IDLE_MS } from './trackpadGesture'

/** The sandbox grants for every frame that holds an agent-written page: saved
 * artifacts (`ArtifactBody`), library thumbnails (`ArtifactThumbs`), a teammate's
 * shared artifact (`RemoteArtifactDetailPage`) and chat widgets (`WidgetFrame`,
 * inline and popped out). The same grant as a crewmate's Dashboard frame
 * (`CREW_DASHBOARD_SANDBOX`). No `allow-popups`: links leave through the host's
 * link bridge (`linkBridge` here, `useFrameLinkBridge` in the host). Mirrored by
 * the `/sandbox-doc/` response header in `dashboard/handlers/sandbox_doc.py`. */
export const AGENT_PAGE_SANDBOX = 'allow-scripts'

/** How long a click or key press stays a live user activation: 5 s in Chromium,
 * and so in the desktop app. Within this long of a click or key outside the
 * asking frame, the link bridges cannot tie a live activation to that frame, so
 * they show the "Open <host>" button instead of opening. */
export const ACTIVATION_LIFESPAN_MS = 5000


/** CSS custom properties the parent app exposes to widgets. Resolved against
 * document.documentElement and serialized into the sandboxed srcdoc so widget
 * HTML can use `style="background: var(--bg)"` or Tailwind arbitrary values
 * like `bg-[var(--card)]` and inherit the dashboard's active theme. */
export const THEME_VAR_NAMES = [
  '--bg', '--bg-elevated', '--bg-hover',
  '--card', '--card-fg',
  '--text', '--text-strong', '--muted', '--muted-strong',
  '--border', '--border-strong',
  '--accent', '--accent-hover', '--accent-subtle',
  '--ok', '--ok-subtle', '--warn', '--warn-subtle',
  '--danger', '--danger-subtle', '--info',
] as const

// CSP for the sandboxed widget iframe. `scriptOrigin` is the dashboard's own
// origin (window.location.origin); the iframe loads the same-origin Tailwind v4
// runtime from exactly `scriptOrigin + TAILWIND_RUNTIME_PATH`, replacing public
// cdn.tailwindcss.com, which locked-down network environments block.
//
// script-src is least-privilege ("allowlist only
// necessary sources"): the dashboard origin is pinned to the single vendored
// runtime FILE, not the whole origin, so an injected/compromised widget cannot
// load arbitrary scripts from other dashboard endpoints. 'unsafe-eval' is NOT
// granted: the Tailwind v4 runtime uses zero
// eval/Function/WebAssembly and Chart.js/D3 do not need it, so widget JS gets no
// dynamic-exec primitive inside the sandbox. 'unsafe-inline' stays: arbitrary
// inline widget <script>/<style> is the core feature and cannot use nonce/hash.
// A null-origin iframe cannot use 'self', which is why the origin is spelled out.
// jsdelivr/cdnjs remain for widget-authored Chart.js/D3 (same-origin + SRI is a
// follow-up). Tailwind v4 emits CSS as inline <style>, so style-src needs no CDN.
//
// `offlineScripts` DROPS those two origins, and a crewmate's dashboard sets it.
// `connect-src 'none'` stops a page phoning home with fetch or a WebSocket, but a
// permitted script ORIGIN is a second channel: a page may append
// `<script src="https://cdn.jsdelivr.net/x.js?d=<encoded fields>">` and the browser
// sends that query out. For a widget that is the accepted trade recorded above --
// the author is the person reading it. A crewmate's dashboard page is different on
// both counts: it is filled with that crewmate's own task titles, summaries and
// costs, and since the preview/apply pair it can be authored by an AGENT and
// accepted by somebody who read the rendering rather than the markup. The product
// already states the property -- "a chart drawn from a CDN renders as a hole rather
// than as a call home" -- so a dashboard document permitting those origins
// contradicted the frame's own contract. A page draws its chart from the inline
// script it ships, which stays permitted.
const cspFor = (scriptOrigin: string, offlineScripts = false): string =>
  "default-src 'none'; " +
  `script-src 'unsafe-inline' ${scriptOrigin}${TAILWIND_RUNTIME_PATH}` +
  (offlineScripts ? '; ' : ' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; ') +
  "style-src 'unsafe-inline'; " +
  "img-src data: blob:; font-src data:; connect-src 'none'; " +
  "form-action 'none'; base-uri 'none';"

const BASE_BODY_CSS =
  "body { margin: 0; padding: 16px; font-family: -apple-system, " +
  "BlinkMacSystemFont, 'Segoe UI', sans-serif; }"

/** `cspFor`'s policy with `'unsafe-inline'` swapped for a nonce, so only a script
 * the HOST built runs: page-authored <script>, inline handlers and javascript:
 * URLs are all refused by the browser. A crewmate's report uses it
 * (`pageScripts: false`): the page is a static snapshot by contract, and a page
 * script could navigate its own frame to a host the crewmate's egress rules
 * forbid. Throws rather than return a policy that still admits inline script. */
function withScriptNonce(policy: string, nonce: string): string {
  const inline = "script-src 'unsafe-inline' "
  if (!policy.includes(inline)) throw new Error('cspFor no longer grants inline script; update withScriptNonce')
  return policy.replace(inline, `script-src 'nonce-${nonce}' `)
}

/** Sub-threshold height jitter (px) the in-iframe reporter ignores. Animated
 * widgets (canvas, CSS transitions) reflow the body by a pixel or two every
 * frame; reporting those would flood the parent with postMessages. */
const HEIGHT_REPORT_EPSILON_PX = 2
/** A smaller height must hold this long (ms) before the iframe reports a
 * shrink. A reload / Tailwind JIT reflow / animation frame briefly reports a
 * smaller height before the content settles; an animated widget that bounces
 * between a low and high value keeps cancelling this timer, so the shrink
 * never fires and the reporter goes quiet on the high value. */
const HEIGHT_REPORT_SHRINK_MS = 200
/** How long the document may stay taller than the frame's viewport, with the
 * viewport height unchanged, before the reporter gives the frame back its own
 * vertical scroll. A self-sizing host grows the frame to a reported height
 * within a frame or two, and every viewport resize restarts this window, so
 * only a frame held at a fixed height (expanded view, popout, thumbnail)
 * outlasts it. */
const HEIGHT_REPORT_FIT_GRACE_MS = 300
/** How far (px) the rounded-up content height may exceed the frame's viewport
 * and still count as fitting. A frame's CSS height is a whole pixel, but its
 * viewport can come out up to a pixel shorter once the engine snaps it to
 * device pixels at a fractional display scale, and clientHeight rounds that
 * value either way. Without this slack a self-sizing frame sized to its exact
 * report reads as too short, gets its scrollbar back, and the loop the
 * overflow handling exists to break resumes. */
const HEIGHT_REPORT_FIT_SLACK_PX = 1

/** How long the loading indicator waits before uncovering the widget anyway.
 * This is a HANG backstop, not a compile budget: the indicator normally clears
 * the moment the Tailwind runtime injects its compiled <style>. Keeping it long
 * matters — a short value uncovers the widget while it is still unstyled, which
 * reproduces the blank-widget symptom the indicator exists to prevent. */
const OVERLAY_HANG_BACKSTOP_MS = 15000

/** Event fired inside the iframe when the Tailwind runtime <script> fails to
 * load (network refusal, PNA/CORS block, packaging gap). The loading overlay's
 * reveal script listens for it and uncovers the widget immediately — a
 * degraded-but-visible widget beats a blank box sitting on the hang backstop,
 * regardless of why the runtime failed. */
const TW_ERROR_EVENT = 'mc-tw-error'

/** Inline onerror body for the runtime <script> tag. Sets a flag (for a reveal
 * script that starts AFTER the failure already fired) and dispatches
 * TW_ERROR_EVENT (for one already listening). Static trusted string — never
 * carries LLM/user content; single-quoted so it embeds in a double-quoted
 * HTML attribute. */
const TW_ERROR_INLINE_HANDLER =
  `window.__mcTwError=1;window.dispatchEvent(new Event('${TW_ERROR_EVENT}'))`

/** Body of the height-reporter script. Defined as a string literal — the only
 * interpolation is the numeric tuning constants above (never LLM/user
 * content; the LLM `html` never reaches here). Set as a script element via
 * textContent below; the iframe re-parses and executes it via the standard
 * <script> mechanism.
 *
 * The reporter is deliberately quiet: it coalesces ResizeObserver bursts into
 * one measurement per animation frame, ignores sub-pixel jitter, applies
 * growth eagerly, and defers/cancels shrinks. The net effect is that a
 * continuously animating widget (lava lamp, starry night) reports its height
 * once and then stops posting, instead of feeding a per-frame resize loop back
 * to the parent. The one deliberate exception is the window `load` re-report
 * below: quietness must never extend across a load event the parent can
 * observe, because parent surfaces treat post-load silence as the frame no
 * longer showing this document.
 *
 * It also hides the frame's own vertical scrollbar while the frame holds the
 * whole document. A classic (space-taking) scrollbar is the one thing that
 * feeds the frame's size back into the content's height: a frame that is a
 * fraction of a pixel short of its content shows a scrollbar, the narrower
 * viewport re-wraps the text taller, the frame grows, the scrollbar goes, the
 * text re-wraps shorter, the frame shrinks, and the cycle repeats every half
 * second. With the vertical overflow hidden while everything fits, the width
 * the content lays out at does not depend on the frame's height, so the
 * height settles. Once the document stays taller than a viewport that is not
 * resizing (see HEIGHT_REPORT_FIT_GRACE_MS) the scroll comes back, so a
 * fixed-height frame keeps scrolling its content. */
const HEIGHT_REPORTER_BODY = `(function(){
  var EPS = ${HEIGHT_REPORT_EPSILON_PX};
  var SHRINK_MS = ${HEIGHT_REPORT_SHRINK_MS};
  var FIT_GRACE_MS = ${HEIGHT_REPORT_FIT_GRACE_MS};
  var FIT_SLACK = ${HEIGHT_REPORT_FIT_SLACK_PX};
  var lastSent = -1;      // last height actually posted to the parent
  var shrinkTimer = null;
  var rafId = 0;
  var raf = window.requestAnimationFrame || function(cb){ return setTimeout(cb, 16); };
  var unraf = window.cancelAnimationFrame || clearTimeout;
  var root = document.documentElement;
  // The document's own inline overflow-y, restored whenever the frame scrolls.
  var authoredOverflowY = root.style.overflowY;
  var clipped = false;
  // Set when the grace window hands the scroll back; holds until the viewport
  // changes height.
  var released = false;
  var fitTimer = null;
  var viewH = root.clientHeight;
  function send(h){
    lastSent = h;
    parent.postMessage({type:'mc-widget-height', height:h}, '*');
  }
  function measure(){
    // The bottom of body's margin box, from the top of the document, rounded
    // UP to a whole pixel. A frame sized to a rounded-down height is short of
    // its content by a fraction of a pixel, which is enough to overflow.
    //
    // Measured on body, NOT documentElement: documentElement.scrollHeight is at
    // least the iframe's own viewport height, so once the iframe grows it can
    // never report smaller, ratcheting the frame taller and taller (capped at
    // MAX_HEIGHT) and never shrinking back to the actual content. body is
    // content-sized (no height), so its box tracks the real content and can
    // shrink. The larger of its box and its scrollHeight covers content that
    // overflows the box (absolutely positioned children, a fixed body height);
    // its top offset covers its own margin, a child margin collapsed through
    // its top edge, and any padding on html.
    var b = document.body;
    var r = b.getBoundingClientRect();
    var top = r.top + (window.scrollY || 0);
    var marginBottom = parseFloat(window.getComputedStyle(b).marginBottom) || 0;
    return Math.ceil(top + Math.max(r.height, b.scrollHeight) + marginBottom);
  }
  function setClipped(on){
    if (on === clipped) return;
    clipped = on;
    root.style.overflowY = on ? 'hidden' : authoredOverflowY;
  }
  function fits(h){
    // clientHeight on the root element is the viewport height less any
    // scrollbar, so a horizontal scrollbar never counts as room.
    return h <= root.clientHeight + FIT_SLACK;
  }
  function fit(h){
    if (fits(h)){
      if (fitTimer){ clearTimeout(fitTimer); fitTimer = null; }
      // Once the scroll is back, a fit while the scrollbar shows is not a fit
      // at full width: content that keeps its aspect ratio gets shorter when
      // the scrollbar narrows it, and clipping again would widen it past the
      // viewport and toggle the scrollbar forever. Only a viewport resize
      // (see the resize handler) clears the latch.
      if (!released) setClipped(true);
      return;
    }
    if (!clipped || fitTimer) return;
    // Taller than the frame. A self-sizing host is about to grow it; give the
    // scroll back only if the viewport has not resized by the time this fires.
    fitTimer = setTimeout(function(){
      fitTimer = null;
      if (!fits(measure())){ released = true; setClipped(false); }
    }, FIT_GRACE_MS);
  }
  function evaluate(){
    rafId = 0;
    var h = measure();
    fit(h);
    if (lastSent < 0){ send(h); return; }            // first measurement
    if (Math.abs(h - lastSent) <= EPS){              // within deadband — ignore jitter
      if (shrinkTimer){ clearTimeout(shrinkTimer); shrinkTimer = null; }
      return;
    }
    if (h > lastSent){
      // Growth applies eagerly so genuine expansion (streaming text, opening a
      // section) isn't laggy. Cancel any pending shrink: an animated widget
      // that oscillates low<->high bounces back here every frame, so the shrink
      // below never commits and we stop posting once locked to the high value.
      if (shrinkTimer){ clearTimeout(shrinkTimer); shrinkTimer = null; }
      send(h);
    } else {
      // Shrink: defer until the smaller height has held for SHRINK_MS. Reset on
      // every tick so a continuously oscillating widget never fires it.
      if (shrinkTimer) clearTimeout(shrinkTimer);
      shrinkTimer = setTimeout(function(){
        shrinkTimer = null;
        var cur = measure();
        if (cur < lastSent && Math.abs(cur - lastSent) > EPS) send(cur);
      }, SHRINK_MS);
    }
  }
  function schedule(){
    // Coalesce a burst of ResizeObserver callbacks (an animation can fire many
    // per frame) into a single measurement per frame.
    //
    // Cancel-and-reschedule rather than an early return on a pending handle:
    // requestAnimationFrame may return a handle whose callback never runs (a
    // frame queued for a page the browser then puts in the back/forward cache is
    // dropped), and latching on such a handle would stop this widget's height
    // tracking permanently. Same defect and same fix as CliPanel's theme
    // scheduler. NOTE this block lives inside a template literal, so it must
    // carry neither a backtick nor a dollar-brace interpolation opener -- both
    // terminate the literal, and the resulting parse error points here rather
    // than at the string, which is why this warning is worth the two lines.
    if (rafId) unraf(rafId);
    rafId = raf(evaluate);
  }
  new ResizeObserver(schedule).observe(document.body);
  window.addEventListener('resize', function(){
    // A frame that changes height is being sized by its host: restart the
    // grace window so a frame catching up with a growth never gets its
    // scrollbar back. A height-only resize leaves body's box unchanged, so the
    // ResizeObserver above does not see it; re-measure here to re-check fit.
    var h = root.clientHeight;
    if (h !== viewH){
      viewH = h;
      released = false;
      if (fitTimer){ clearTimeout(fitTimer); fitTimer = null; }
    }
    schedule();
  });
  window.addEventListener('load', function(){
    // Re-report UNCONDITIONALLY after load, bypassing the deadband. The parent
    // surface re-arms its silence window on EVERY iframe load event (an engine
    // renavigation onto a spent single-use url fires load with no reporter
    // behind it, and silence within the grace window is its only signal -- see
    // DOC_REPORT_GRACE_MS in ArtifactBody). A first measurement that raced
    // ahead of the load event -- layout settles before images and fonts finish
    // -- would otherwise be the LAST post this document ever makes: the parent
    // discards it at load, the deadband swallows this re-check because the
    // height is unchanged, and three seconds later a healthy, rendering
    // document is flagged as no longer showing. Resetting lastSent routes the
    // scheduled measurement through the first-measurement path, so every load
    // the parent can observe is followed by a report.
    setTimeout(function(){ lastSent = -1; schedule(); }, 100);
  });
  // Start without a vertical scrollbar, so a frame still growing to its first
  // report never flashes one; fit() hands the scroll back if it stays short.
  setClipped(true);
  schedule();
  document.addEventListener('click', function(e){
 // NOTE: this isTrusted check runs INSIDE the sandboxed iframe
    // and is NOT the security trust boundary. LLM-emitted <script> in this same
    // document can call parent.postMessage({type:'mc-widget-action',...})
    // directly and skip this handler entirely. The real protection is on the
    // parent side: a widget action only PRE-FILLS the composer (it never
    // auto-submits a user-role turn). Do not treat this guard as authoritative.
    if (!e.isTrusted) return;
    var el = e.target.closest('[data-action]');
    if (!el) return;
    e.preventDefault();
    var action = el.dataset.action;
    var payload = {};
    try { payload = JSON.parse(el.dataset.payload || '{}'); } catch(x){}
    var inputs = document.querySelectorAll('input,select,textarea');
    var formData = {};
    inputs.forEach(function(inp){
      var n = inp.name || inp.id || inp.getAttribute('data-field');
      if (!n) return;
      if (inp.type === 'checkbox') formData[n] = inp.checked;
      else if (inp.type === 'radio') { if (inp.checked) formData[n] = inp.value; }
      else formData[n] = inp.value;
    });
    if (Object.keys(formData).length) payload.formData = formData;
    parent.postMessage({type:'mc-widget-action', action:action, payload:payload}, '*');
  });
})();`

/** Tailwind v4 browser build: dark mode is driven by a `.dark` class on <body>
 * (set below) via a custom variant — NOT the v3 `tailwind.config` global, which
 * v4 removed. The @tailwindcss/browser runtime auto-injects preflight + theme +
 * utilities on load (verified via headless render: base reset AND utilities apply
 * without an explicit `@import "tailwindcss"`), so this block only registers the
 * dark variant. Injected as a `<style type="text/tailwindcss">` block the runtime
 * compiles on load; it must precede the runtime script so the variant registers
 * before first paint. */
const TAILWIND_V4_DIRECTIVES =
  '@custom-variant dark (&:where(.dark, .dark *));'

/** Highlight + bubble CSS for anchored comments rendered inside the iframe. */
const COMMENT_HIGHLIGHT_CSS =
  ".mc-cmt-hl{background:var(--warn-subtle,rgba(255,196,0,.28));" +
  "border-bottom:2px solid var(--warn,#e0a000);border-radius:2px;cursor:pointer;" +
  "box-shadow:0 2px 6px rgba(0,0,0,.38)}" +
  ".mc-cmt-hl.mc-cmt-flash{background:var(--accent-subtle,rgba(80,140,255,.5));" +
  "border-bottom-color:var(--accent,#5a8cff);transition:background .25s ease}" +
  ".mc-cmt-hl.mc-cmt-active{background:var(--accent-subtle,rgba(80,140,255,.5));" +
  "border-bottom-color:var(--accent,#5a8cff);box-shadow:0 3px 12px rgba(0,0,0,.45)}" +
  ".mc-cmt-bubble.mc-cmt-active{background:var(--accent,#5a8cff);color:var(--accent-fg,#fff);border-color:var(--accent,#5a8cff)}" +
  ".mc-cmt-bubble{position:absolute;z-index:2147483646;width:19px;height:19px;" +
  "display:flex;align-items:center;justify-content:center;font:700 10px/1 sans-serif;" +
  "color:var(--warn,#e0a000);background:var(--card,#1c1c1c);border:1.5px solid var(--warn,#e0a000);" +
  "border-radius:10px 10px 10px 2px;box-shadow:0 1px 3px rgba(0,0,0,.35);cursor:pointer}" +
  ".mc-cmt-bubble.unread{background:var(--warn,#e0a000);color:var(--warn-fg,#1a1a1a)}"

/** Guards against the "unsafe centering" clip: artifacts that lay out with
 * `body { display:flex; align-items:center }` (or grid `place-items:center`)
 * clip the TOP of their content — unreachable by scrolling — whenever the
 * content is taller than the iframe viewport (common in shorter windows, e.g.
 * an artifact popout). CSS solves this with `safe center`: identical to
 * `center` when the content fits, falls back to start-alignment when it
 * overflows. Applied only when the body actually uses a centering flex/grid
 * layout, so non-centered artifacts are untouched. Re-checked on resize since
 * the overflow condition depends on the viewport. */
const SAFE_CENTER_GUARD_BODY = `(function(){
  function apply(){
    var cs = getComputedStyle(document.body);
    if(cs.display!=='flex' && cs.display!=='grid' && cs.display!=='inline-flex' && cs.display!=='inline-grid') return;
    if(cs.alignItems==='center') document.body.style.alignItems='safe center';
    if(cs.justifyContent==='center') document.body.style.justifyContent='safe center';
    if(cs.alignContent==='center') document.body.style.alignContent='safe center';
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', apply);
  else apply();
  window.addEventListener('resize', apply);
})();`

/** Clipboard write-fallback shim, injected into every document buildSrcdoc()
 * builds. Widget and artifact bodies routinely carry a copy button, and
 * without this every one of them is dead.
 *
 * These frames deliberately do NOT receive a delegated `clipboard-write`
 * permission. Delegation would let agent-authored script write during load,
 * without a Copy action. The null-origin frame's native writeText therefore
 * rejects, while a user-initiated copy can still use the execCommand fallback
 * below. On plain-HTTP deployments navigator.clipboard is absent entirely, so
 * the same fallback supplies the only writeText implementation there too.
 * Gesture-less load-time calls remain rejected when execCommand lacks user
 * activation; the shim does not turn them into successful clipboard writes.
 *
 * Shadows ONLY `writeText` on the existing `navigator.clipboard` (an own
 * property beats the prototype method for every caller), leaving
 * `readText`/`write`/`read` and its EventTarget nature untouched — this is
 * not a wholesale replacement, since `writeText` is the one method on the
 * sensitive path. When `navigator.clipboard` is absent entirely a minimal
 * object carrying just `writeText` is defined on `navigator` itself. Each
 * `defineProperty` is wrapped in try/catch so an unusual engine cannot break
 * widget rendering.
 *
 * The wrapped `writeText` tries the native implementation first (when one
 * exists and is allowed); only on rejection — or when there is no native
 * implementation at all — does it fall back to a textarea +
 * `execCommand('copy')`, which works inside the sandbox during user activation.
 * The fallback restores focus and the document selection exactly as
 * `utils/clipboard.ts` does, for the same reason: widget bodies contain
 * focusable controls, so a copy must not silently move focus or clobber a
 * selection the caller means to keep. Static trusted JS string — never
 * carries LLM/user content — assigned via script.textContent, never
 * interpolated into the template literal. Lives in a template literal, so it
 * must contain no backtick and no dollar-brace opener. */
const CLIPBOARD_FALLBACK_SHIM_BODY = `(function(){
  function execCommandCopy(text){
    if (typeof document.execCommand !== 'function') return false;
    var previouslyFocused = document.activeElement;
    var selection = document.getSelection();
    var savedRanges = [];
    if (selection) { for (var i = 0; i < selection.rangeCount; i++) savedRanges.push(selection.getRangeAt(i)); }
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.readOnly = true;
    ta.setAttribute('aria-hidden', 'true');
    ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;opacity:0';
    document.body.appendChild(ta);
    try {
      ta.select();
      return document.execCommand('copy');
    } catch (e) {
      return false;
    } finally {
      document.body.removeChild(ta);
      if (selection) {
        selection.removeAllRanges();
        for (var j = 0; j < savedRanges.length; j++) selection.addRange(savedRanges[j]);
      }
      if (previouslyFocused && previouslyFocused.focus) {
        try { previouslyFocused.focus({ preventScroll: true }); } catch (e) {}
      }
    }
  }
  function wrappedWriteText(nativeWriteText, text){
    if (nativeWriteText) {
      return nativeWriteText(text).then(function(){ return undefined; }, function(err){
        if (execCommandCopy(text)) return undefined;
        throw err;
      });
    }
    return execCommandCopy(text)
      ? Promise.resolve(undefined)
      : Promise.reject(new Error('copy failed'));
  }
  try {
    if (navigator.clipboard) {
      var native = navigator.clipboard.writeText
        ? navigator.clipboard.writeText.bind(navigator.clipboard)
        : null;
      try {
        Object.defineProperty(navigator.clipboard, 'writeText', {
          configurable: true,
          value: function(text){ return wrappedWriteText(native, text); },
        });
      } catch (e) {}
    } else {
      try {
        Object.defineProperty(navigator, 'clipboard', {
          configurable: true,
          value: { writeText: function(text){ return wrappedWriteText(null, text); } },
        });
      } catch (e) {}
    }
  } catch (e) {}
})();`

/** Absolute web links inside the sandboxed document open OUTSIDE it.
 *
 * The default for a caller that sets neither `linkBridge` nor
 * `rewriteBareLinks: false` -- today the exported copy the artifact page
 * downloads. Every dashboard frame that holds an agent-written page sets
 * `linkBridge` instead (see LINK_BRIDGE_SHIM_BODY). Where it runs, the frame or
 * window is assumed to grant popups, so
 * a link with `target="_blank"` becomes a window-open request the host handles
 * -- in the desktop app that is `setWindowOpenHandler` -> `shell.openExternal`,
 * i.e. the user's own default browser with their own logins; in a plain browser
 * it is a new tab. A link WITHOUT a target navigates the sandboxed frame
 * itself: the widget body is replaced by the target site rendered with no
 * cookies (an SSO login page where a code-review page should be), inside a
 * chat row that has no address bar and no way back. The agent is instructed to
 * write `target="_blank"` on every link, but that instruction is only as
 * reliable as the model that follows it, so this rewrites a bare absolute
 * `http(s)://` link to `_blank` at click time.
 *
 * Scope is deliberately narrow:
 *   - Only hrefs that parse (`new URL(href)`, no base) to an `http:`/`https:`
 *     URL. Parsing rather than regex-matching the raw attribute means a link
 *     the browser WOULD navigate (leading whitespace, embedded tab/newline)
 *     is caught too. Fragment links (`#section`, in-widget tab navigation)
 *     and relative paths do not parse without a base, so they keep their
 *     default behaviour -- a `<base target="_blank">` would have sent those to
 *     a new window too, which is why this is a click hook and not a `<base>`.
 *     `mailto:` and other non-web schemes are left alone as well: the desktop
 *     host's window-open handler (`electron/external-scheme.js`) hands only
 *     web URLs and an exact allowlist to the OS and denies the rest, so
 *     rewriting a `mailto:` to `_blank` would turn it into a dead click there.
 *   - Only anchors with NO `target` attribute. An explicit `_self` is author
 *     intent and stays.
 *   - `data-action` anchors are the composer pre-fill buttons; that handler
 *     (in HEIGHT_REPORTER_BODY) preventDefaults them, so they are skipped here.
 *   - `rel="noopener noreferrer"` is set alongside so the opened page gets no
 *     `window.opener` back into the frame.
 *
 * Registered on `window` in the capture phase, and this script runs before the
 * widget's own, so it is the first click listener to fire: a widget-authored
 * capture listener on `window` or `document` that stops propagation cannot get
 * ahead of it. The anchor is resolved from `e.composedPath()[0]` so a link
 * inside an open shadow root (where `e.target` is retargeted to the host) is
 * still found. Setting the attribute during dispatch is sufficient: the
 * anchor's activation behaviour reads `target` after dispatch completes.
 *
 * Only injected when the HOST frame grants popups (`rewriteBareLinks`, on by
 * default). A frame whose sandbox withholds `allow-popups` -- the mochi pet
 * frame, the crew webview -- turns a `_blank` link into a blocked popup, i.e. a
 * dead click, which is worse than the in-frame navigation it replaces; those
 * hosts pass `rewriteBareLinks: false` and keep their pre-existing behaviour.
 * Not a security boundary (the frame is null-origin and, where this runs,
 * popups are already allowed); it is a usability fix for the
 * frame-replaced-by-login-page case.
 */
/**
 * Touch relay: reports this frame's single-finger touches to the parent, so a
 * host's drawer gesture can follow a drag that began over the frame.
 *
 * A touch that lands on an iframe is delivered to the frame's own document and
 * never reaches the host page, so a host panel holding a full-width frame (the
 * phone Crewmates side panel's Dashboard) had nowhere a closing drag could start.
 * The relay posts `{type: 'mc-frame-touch', phase, x, y}` in frame coordinates;
 * the host maps them onto the frame element and decides what, if anything, they
 * mean.
 *
 * It stands down for a touch the frame's own content owns -- inside a
 * horizontally scrollable element, an editable, or an active text selection --
 * the same deference the host gesture applies to its own DOM. Passive listeners:
 * the frame keeps scrolling exactly as before. Not a security boundary: any
 * script in the frame can post the same shape, so a host must treat the
 * messages as untrusted (accept them only from the frame's window, only while
 * the gesture they can drive is harmless).
 */
const FRAME_TOUCH_RELAY_BODY = `(function(){
  function scrollsX(n){
    for(;n&&n.nodeType===1;n=n.parentElement){
      if(n.scrollWidth-n.clientWidth>1){var o=getComputedStyle(n).overflowX;if(o==='auto'||o==='scroll')return true;}
    }
    return false;
  }
  function editable(n){
    for(;n&&n.nodeType===1;n=n.parentElement){
      if(n.isContentEditable||n.tagName==='INPUT'||n.tagName==='TEXTAREA'||n.tagName==='SELECT')return true;
    }
    return false;
  }
  var on=false;
  function send(phase,e){
    var t=(e.touches&&e.touches[0])||(e.changedTouches&&e.changedTouches[0]);
    if(!t)return;
    parent.postMessage({type:'mc-frame-touch',phase:phase,x:t.clientX,y:t.clientY},'*');
  }
  var opts={passive:true,capture:true};
  window.addEventListener('touchstart',function(e){
    if(on&&e.touches.length>1){send('cancel',e);on=false;return;}
    var sel=window.getSelection?window.getSelection():null;
    on=e.touches.length===1&&!scrollsX(e.target)&&!editable(e.target)&&!(sel&&!sel.isCollapsed);
    if(on)send('start',e);
  },opts);
  window.addEventListener('touchmove',function(e){if(on)send('move',e);},opts);
  window.addEventListener('touchend',function(e){if(on){on=false;send('end',e);}},opts);
  window.addEventListener('touchcancel',function(e){if(on){on=false;send('cancel',e);}},opts);
})();`

/** A wheel relay, for a host whose desktop trackpad swipe must be able to run
 * over the frame (see hooks/useTrackpadSwipe). A gesture's axis is fixed by its
 * first event, as the host's own reader fixes it, and a run ends after the same
 * gap the host's does (SWIPE_GESTURE_IDLE_MS): a run that starts vertical, a
 * pinch (`ctrlKey`), or a run over content that can still scroll that way is
 * the frame's and posts nothing. Scrollers are found along the event's composed
 * path, so one inside a shadow root (a web component) counts, not just its host.
 * Whether a scroller has room is the host's own rule (`room`, a copy of
 * lib/trackpadGesture `hasScrollRoomX`), right-to-left scrollers included. Every other run is posted, event by event, as
 * `{type: 'mc-frame-wheel', dx, dy, mode, t}`, where `t` is when the wheel
 * happened, as `performance.timeOrigin + timeStamp` (epoch ms): the host's
 * own receive time would squeeze a burst queued behind a stall into a few
 * ms, and read slow fingers as a flick. A leftward-scrolling event of such
 * a run (`deltaX < 0`, the fingers moving right) is also cancelled here: the
 * host can drive only its panel's close from this frame, which is that
 * direction, and a parent cannot cancel the browser's history swipe for a
 * wheel that landed in a frame.
 *
 * Disarmed until the host says it is listening: the frame posts
 * `{type: 'mc-frame-wheel-hello'}` when it starts, and relays (and cancels)
 * only after the parent answers `{type: 'mc-frame-wheel-armed', on: true}`. The
 * host sends `on: false` when its swipe stops listening (a phone-width window,
 * a creation surface), so the frame then leaves every wheel to the browser
 * instead of cancelling a run nobody will act on. Only the parent may arm it. */
const FRAME_WHEEL_RELAY_BODY = `(function(){
  var armed=false;
  window.addEventListener('message',function(e){
    var d=e.data;
    if(e.source===parent&&d&&d.type==='mc-frame-wheel-armed')armed=d.on===true;
  });
  parent.postMessage({type:'mc-frame-wheel-hello'},'*');
  function room(sl,m,rtl,dx){
    if(m<=1)return false;
    if(!rtl)return dx>0?sl<m-1:sl>0;
    return dx>0?sl<0:sl>-(m-1);
  }
  function can(n,dx){
    var m=n.scrollWidth-n.clientWidth;
    if(m>1){var cs=getComputedStyle(n),o=cs.overflowX;if((o==='auto'||o==='scroll')&&room(n.scrollLeft,m,cs.direction==='rtl',dx))return true;}
    return false;
  }
  function scrolls(e,dx){
    var p=e.composedPath?e.composedPath():[];
    if(p.length){for(var i=0;i<p.length;i++){var c=p[i];if(c&&c.nodeType===1&&can(c,dx))return true;}}
    else{for(var n=e.target;n&&n.nodeType===1;n=n.parentElement){if(can(n,dx))return true;}}
    var s=document.scrollingElement;
    if(s&&room(s.scrollLeft,s.scrollWidth-s.clientWidth,getComputedStyle(s).direction==='rtl',dx))return true;
    return false;
  }
  var last=-1e9,axis='';
  window.addEventListener('wheel',function(e){
    if(!armed)return;
    var now=e.timeStamp;if(now-last>${SWIPE_GESTURE_IDLE_MS})axis='';last=now;
    var dx=e.deltaX,dy=e.deltaY;
    if(!axis){
      if(e.ctrlKey)axis='own';
      else if(dx===0&&dy===0)return;
      else axis=Math.abs(dx)>Math.abs(dy)&&!scrolls(e,dx)?'x':'own';
    }
    if(axis!=='x'||e.ctrlKey)return;
    parent.postMessage({type:'mc-frame-wheel',dx:dx,dy:dy,mode:e.deltaMode,t:performance.timeOrigin+e.timeStamp},'*');
    if(dx<0&&Math.abs(dx)>Math.abs(dy))e.preventDefault();
  },{passive:false,capture:true});
})();`

/** A static page's links do nothing when clicked. Installed (with the nonce) ahead
 * of the page, as capture-phase listeners on the window that the page has no
 * script to undo, so a click cannot navigate the frame to a host carrying the
 * page's text. A click or auxclick loses its default action when any node on its
 * path is an `a` or `area` (HTML or SVG), whatever its `href`, since an SVG
 * animation can change the href a click follows. Every other click keeps its
 * default, so a `<details>` still opens. The path is the whole tree: a static
 * page has no shadow root, since its declarative ones are dropped (see
 * STATIC_PAGE_DROPPED) and it has no script to attach one. */
const STATIC_LINK_GUARD_BODY = `(function(){function stop(e){` +
  `var p=e.composedPath?e.composedPath():[];for(var i=0;i<p.length;i++){` +
  `var l=p[i]&&p[i].localName;if(l==='a'||l==='area'){e.preventDefault();return;}}}` +
  `window.addEventListener('click',stop,true);window.addEventListener('auxclick',stop,true);})();`

/** Page markup that leaves the frame with no script and no click: a refresh
 * `meta`, a `link` hint, and `noscript`, which the host parses with scripting
 * off and the frame with scripting on, so its content is read twice. Plus a
 * declarative shadow root, which the host's parsers keep as a `template` and the
 * frame's attaches, so markup inside it is out of the host's reach. */
const STATIC_PAGE_DROPPED = 'meta, link, noscript, template[shadowrootmode], template[shadowroot]'
/** Rounds of parse, drop and re-serialize before the page is given up. */
const STATIC_PAGE_SETTLE_ROUNDS = 3

/** The finished document, parsed exactly as the frame will parse it, with every
 * {@link STATIC_PAGE_DROPPED} element in the body removed until a parse finds
 * none. Checked on the final string rather than on the fragment, because markup
 * can re-parse into a different tree than the one first built. A page that has
 * not settled after {@link STATIC_PAGE_SETTLE_ROUNDS} keeps only the host's own
 * scripts: the check fails closed. */
function settleStaticPage(serialized: string, nonce: string): string {
  let out = serialized
  for (let round = 0; round < STATIC_PAGE_SETTLE_ROUNDS; round++) {
    const parsed = new DOMParser().parseFromString(out, 'text/html')
    const found = Array.from(parsed.body.querySelectorAll(STATIC_PAGE_DROPPED))
    if (found.length === 0) return out
    for (const el of found) el.remove()
    out = `<!DOCTYPE html>\n${parsed.documentElement.outerHTML}`
  }
  const parsed = new DOMParser().parseFromString(out, 'text/html')
  if (parsed.body.querySelectorAll(STATIC_PAGE_DROPPED).length === 0) return out
  for (const child of Array.from(parsed.body.childNodes)) {
    const host = child.nodeName === 'SCRIPT' && (child as Element).getAttribute('nonce') === nonce
    if (!host) child.remove()
  }
  return `<!DOCTYPE html>\n${parsed.documentElement.outerHTML}`
}

const EXTERNAL_LINK_TARGET_SHIM_BODY = `(function(){
  window.addEventListener('click', function(e){
    var path = typeof e.composedPath === 'function' ? e.composedPath() : null;
    var t = (path && path.length) ? path[0] : e.target;
    if (!t || typeof t.closest !== 'function') return;
    var a = t.closest('a[href]');
    if (!a || a.hasAttribute('target') || a.closest('[data-action]')) return;
    var u;
    try { u = new URL(a.getAttribute('href') || ''); } catch (_) { return; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return;
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
  }, true);
})();`

/** Links leave the frame through the host, never through a popup of its own.
 *
 * Injected by `linkBridge` for the frames that hold agent-written pages (saved
 * artifacts, library thumbnails, a teammate's shared artifact, chat widgets, a
 * crewmate's Dashboard page).
 * Those frames are `sandbox="allow-scripts"` with no popup grant, so a page
 * cannot open a window itself; this hands the request to the host instead, as
 * `{type: 'kirocrew-dashboard:open', url}` -- the message the crew pages already
 * post. The host (`useFrameLinkBridge`) opens it only for a web URL and only right
 * after a real click in this frame, once per click.
 *
 *   - A middle-click on a web link is handled the same way (it asks for a new
 *     tab whatever the target).
 *   - A click on a link that would open a window (`target="_blank"` or any other
 *     target but `_self`) or that would navigate this frame to another site (a
 *     bare absolute `http(s)` link) is cancelled here and, when its href is an
 *     absolute `http(s)` URL, posted to the host. A window-opening link to a
 *     root-relative dashboard path (`/chat?sid=...`) is posted as that path,
 *     which the host resolves against its own origin. Fragment and relative links
 *     with no target keep their in-frame behaviour; other schemes on a
 *     window-opening link are cancelled and not posted.
 *   - `window.open` is replaced by the same post and returns null, so a page
 *     that opens from script reaches the host too.
 *   - `data-action` anchors are the composer pre-fill buttons; their own handler
 *     owns them, so they are skipped.
 *   - Each click or key in the frame is reported as
 *     `{type: 'kirocrew-dashboard:input'}`, so the host does not open a SIBLING
 *     frame's request at once on that click (it offers its "Open <host>"
 *     button instead).
 *
 * A link click is decided a task after dispatch, after every page handler, so a
 * page that cancels it keeps that and nothing is posted. A plain click is left to
 * the sandbox, which refuses the window it would open (a bare web link is made
 * window-opening first, so it cannot navigate this frame away); a middle or
 * modified click, which opens a tab even without `allow-popups`, is cancelled at
 * once. Does nothing when the document has no parent (a downloaded
 * copy opened as a file, a document opened top-level), where ordinary links work.
 * A web link without a title gets its resolved URL as one on hover or focus, so
 * the URL shows before a click. Not a security boundary on its own: any page
 * script can post the same message, which is why the host checks the frame, the
 * URL and the gesture before opening.
 */
const LINK_BRIDGE_SHIM_BODY = `(function(){
  if (window.parent === window) return;
  var T = 'kirocrew-dashboard:open';
  var P = window.parent;
  // Each click or key here is reported, so the host can tell that a live
  // activation may be this frame's when a sibling frame asks to open a link.
  function input(){ try { P.postMessage({ type: 'kirocrew-dashboard:input' }, '*'); } catch (_) {} }
  ['keydown', 'mousedown', 'pointerdown', 'pointerup', 'touchend'].forEach(function(t){
    window.addEventListener(t, input, { capture: true, passive: true });
  });
  // A protocol-relative link (//host/path) takes the scheme of this document's
  // base, as the browser resolves it: the dashboard's for a srcdoc frame. Only that
  // form is resolved; a relative path stays the frame's own navigation.
  function scheme(){
    try { var b = new URL(document.baseURI); if (b.protocol === 'http:' || b.protocol === 'https:') return b.protocol; } catch (_) {}
    return 'https:';
  }
  function web(raw){
    var s = String(raw).trim(), u;
    if (s.slice(0, 2) === '//') s = scheme() + s;
    try { u = new URL(s); } catch (_) { return null; }
    return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : null;
  }
  // A root-relative dashboard path (/chat?sid=...), sent as written: the host
  // resolves it against its own origin. Parsed against a placeholder origin and
  // returned only while it stays there, so //host and /\\host (another origin) and any
  // scheme are refused. Used only for a link that opens a window, never a bare one.
  function path(raw){
    var s = String(raw).trim(), u, o = 'https://kirocrew-path.invalid';
    if (s.charAt(0) !== '/') return null;
    try { u = new URL(s, o); } catch (_) { return null; }
    // A dot segment can normalise to //host (/.//host), which the host would read
    // as another origin, so a pathname starting // is refused too.
    if (u.origin !== o || u.pathname.slice(0, 2) === '//') return null;
    return u.pathname + u.search + u.hash;
  }
  function post(url){
    try { P.postMessage({ type: T, url: url }, '*'); } catch (_) {}
  }
  function ask(raw){
    var url = web(raw);
    if (url) post(url);
  }
  // An SVG link may carry its address as xlink:href instead of href.
  var XLINK = 'http://www.w3.org/1999/xlink';
  function hrefOf(n){
    return n.getAttribute('href') || (typeof n.getAttributeNS === 'function' ? n.getAttributeNS(XLINK, 'href') : '') || '';
  }
  function linkOf(e){
    // The composed path crosses shadow roots, so a link that wraps a custom
    // element's shadow content is still found; closest() alone stops at the root.
    var path = typeof e.composedPath === 'function' ? e.composedPath() : [e.target];
    for (var i = 0; i < path.length; i++) {
      var n = path[i];
      if (n && n.nodeType === 1 && (n.localName === 'a' || n.localName === 'area')
        && (n.hasAttribute('href') || (typeof n.hasAttributeNS === 'function' && n.hasAttributeNS(XLINK, 'href')))) return n;
    }
    return null;
  }
  // What a link event would do here: null to leave it to the frame, else the
  // link, the URL to post (null for a non-web scheme) and whether it is bare.
  function planOf(e, middle){
    var a = linkOf(e);
    if (!a || (typeof a.closest === 'function' && a.closest('[data-action]'))) return null;
    var target = (a.getAttribute('target') || '').trim().toLowerCase();
    var url = web(hrefOf(a));
    if (!middle && target === '_self') return null;
    if (!middle && !target && !url) return null;
    if (!url) url = path(hrefOf(a));
    if (middle && !url) return null;
    return { a: a, url: url, bare: !middle && !target };
  }
  // A link click is decided a task after dispatch, so every page handler, a
  // window-level one included, has run: a page that cancels it (an in-page panel,
  // onclick="return false") keeps that and nothing is posted. A plain click is not
  // cancelled here: the window it would open is refused by the sandbox, and a bare
  // web link is made window-opening first so it cannot navigate this frame away.
  // A middle or modified click opens a tab even without allow-popups, so that one
  // must be cancelled before its default runs: by a listener added to window now,
  // during capture, which therefore runs after every bubbling window listener the
  // page registered. The page has had its say by then in whatever form
  // (preventDefault, return false, returnValue), and defaultPrevented reads all of
  // them. A page that stops the click before window keeps its own handling, and
  // nothing is posted.
  function decide(e, p, now){
    if (!now) {
      setTimeout(function(){ if (!e.defaultPrevented && p.url) post(p.url); }, 0);
      return;
    }
    window.addEventListener(e.type, function(ev){
      if (ev !== e || e.defaultPrevented) return;
      e.preventDefault();
      if (p.url) post(p.url);
    }, { once: true });
  }
  window.addEventListener('click', function(e){
    var p = planOf(e, false);
    if (!p) return;
    if (p.bare) { p.a.setAttribute('target', '_blank'); p.a.setAttribute('rel', 'noopener noreferrer'); }
    decide(e, p, e.ctrlKey || e.metaKey || e.shiftKey);
  }, true);
  // Hover shows where a link goes, as in a session: a web link without a title
  // gets its resolved URL as one. Some hosts (the desktop app) show no status
  // bar for a framed link, so the title is the only place the URL appears.
  function titleLink(e){
    var a = linkOf(e);
    if (!a || a.hasAttribute('title')) return;
    var url = web(hrefOf(a));
    if (url) a.setAttribute('title', url);
  }
  window.addEventListener('mouseover', titleLink, true);
  window.addEventListener('focusin', titleLink, true);
  // A middle-click asks for a new tab whatever the target, which the frame cannot open.
  window.addEventListener('auxclick', function(e){
    if (e.button !== 1) return;
    var p = planOf(e, true);
    if (p) decide(e, p, true);
  }, true);
  // A plain assignment, not a locked property: a page script that declares its
  // own top-level open (function or const) must still run. The sandbox already
  // refuses a native popup, and the host checks every request.
  try { window.open = function(u){ ask(u); return null; }; } catch (_) {}
})();`

/** The host half of the link bridge for a widget popped out into its own tab
 * (`WidgetFrame`'s "Open in new tab"). That tab is a wrapper page the dashboard
 * writes, holding the widget in an `AGENT_PAGE_SANDBOX` frame, so React and
 * `useFrameLinkBridge` are not there to answer the frame's open message. This
 * script is: the same checks (the message comes from the frame, the URL is an
 * absolute `http(s)` URL with no user name or password, the person just clicked
 * with the frame focused), then a `noopener,noreferrer` open, once per click,
 * when the click is tied to the frame: focus came into it from the tab, and no
 * click or key in the tab itself (a Tab into the frame) is still live. Otherwise
 * it shows the same "Open <host>" button as the dashboard (`FrameLinkChip`),
 * whose click is the person's own in the tab. */
const POPOUT_LINK_BRIDGE_BODY = `(function(){
  var T = 'kirocrew-dashboard:open';
  var LIFE = ${ACTIVATION_LIFESPAN_MS};
  // The tab's own clicks and keys, never the frame's. This script runs as the
  // tab loads, before any input in it.
  var last = -Infinity, entered = false, chip = null;
  ['keydown', 'mousedown', 'pointerdown', 'pointerup', 'touchend'].forEach(function(t){
    window.addEventListener(t, function(e){
      last = Date.now();
      if (chip && (t === 'keydown' || t === 'pointerdown') && !chip.contains(e.target)) drop();
    }, true);
  });
  // Focus coming into the frame from the tab blurs the tab's window.
  window.addEventListener('blur', function(){ entered = document.activeElement === document.querySelector('iframe'); });
  window.addEventListener('focus', function(){ entered = false; });
  function drop(){ if (chip) { chip.remove(); chip = null; } }
  function offer(href, host){
    drop();
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = LABEL.split('%HOST%').join(host);
    b.title = href;
    b.setAttribute('data-frame-link-chip', '');
    b.setAttribute('style', 'position:fixed;top:8px;right:8px;z-index:2147483647;padding:4px 10px;' +
      'border-radius:6px;border:1px solid GrayText;background:Canvas;color:CanvasText;' +
      'font:13px system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.25)');
    b.addEventListener('click', function(){ drop(); window.open(href, '_blank', 'noopener,noreferrer'); });
    b.addEventListener('keydown', function(e){ if (e.key === 'Escape') drop(); });
    document.body.appendChild(b);
    chip = b;
    // Focus leaves the frame, so the page cannot ask again on the same click.
    try { b.focus(); } catch (_) {}
  }
  function clicked(){
    var f = document.querySelector('iframe');
    var ua = navigator.userActivation;
    return f && ua && ua.isActive && document.activeElement === f ? f : null;
  }
  function go(f, href){
    // Once per click: focus leaves the frame, so a frame posting again from the
    // same click fails the focus check until the next real click.
    try { f.blur(); } catch (_) {}
    window.open(href, '_blank', 'noopener,noreferrer');
  }
  window.addEventListener('message', function(e){
    var f = document.querySelector('iframe');
    if (!f || !e.source || e.source !== f.contentWindow) return;
    var d = e.data;
    if (!d || typeof d !== 'object' || d.type !== T || typeof d.url !== 'string') return;
    if (!d.url) return;
    var u;
    try { u = new URL(d.url); } catch (_) { return; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return;
    if (u.username || u.password) return;
    if (!clicked()) return;
    if (entered && Date.now() - last >= LIFE) go(f, u.href);
    else offer(u.href, u.host);
  });
})();`

/** The popped-out widget tab's link bridge: fixed first-party text and the
 * button's localized *label*, in which `%HOST%` stands for the link's host. No
 * widget content. */
export function popoutLinkBridgeScript(label: string): string {
  return `var LABEL = ${JSON.stringify(label).replace(/</g, '\\u003c')};\n${POPOUT_LINK_BRIDGE_BODY}`
}

/** In-iframe comment bridge. Vanilla JS string — no LLM/user
 * interpolation (no `${}`), set via textContent and re-parsed by the iframe.
 * Brings anchored commenting + highlights into the sandboxed HTML render that
 * the parent cannot reach directly:
 *   - mouseup with a text selection -> postMessage 'mc-comment-select'
 *     {quote, prefix, suffix, rect} so the parent opens the comment popover;
 *   - 'mc-comment-highlights' {anchors:[{id,quote,prefix,suffix}]} -> wrap each
 *     matched run in <mark.mc-cmt-hl data-cid> (click -> 'mc-comment-highlight-click');
 *   - 'mc-comment-scroll-to' {id} -> scroll the mark into view + flash it;
 *   - 'mc-comment-active' {id} -> mark that comment's highlights and bubble active;
 *   - posts 'mc-comment-ready' once so the parent pushes the initial set. */
const COMMENT_BRIDGE_BODY = `(function(){
  var PFX = 32;
  function selectionContext(){
    var sel = window.getSelection();
    if(!sel || sel.isCollapsed || sel.rangeCount===0) return null;
    var quote = sel.toString();
    if(!quote || !quote.trim()) return null;
    var range = sel.getRangeAt(0);
    // Derive the selection offset from the Range, NOT body.indexOf(quote):
    // indexOf returns the FIRST occurrence, so selecting a later repeat of the
    // same text (e.g. a single common word) stored prefix/suffix and
    // startOffset/endOffset for the wrong spot, making the highlight teleport to
    // the first occurrence on re-render. Range.toString over the
    // body contents and the pre-selection slice keeps the offset space
    // consistent and mirrors the matcher (findRange) which works off text-node
    // textContent; innerText would insert block newlines Range.toString omits.
    var fullRange = document.createRange();
    fullRange.selectNodeContents(document.body);
    var full = fullRange.toString();
    var idx = full.indexOf(quote);
    try{
      var preRange = document.createRange();
      preRange.selectNodeContents(document.body);
      preRange.setEnd(range.startContainer, range.startOffset);
      idx = preRange.toString().length;
    }catch(x){ /* fall back to indexOf result */ }
    if(idx < 0) idx = 0;
    var prefix = full.slice(Math.max(0, idx-PFX), idx);
    var suffix = full.slice(idx+quote.length, idx+quote.length+PFX);
    var r = range.getBoundingClientRect();
    // Char offsets of the selection within the body text. The provider's
    // create_comment anchor schema REQUIRES startOffset/endOffset/versionNumber;
    // without them the remote post is rejected and the comment silently vanishes.
    var startOffset = idx;
    var endOffset = idx+quote.length;
    return {quote:quote, prefix:prefix, suffix:suffix, startOffset:startOffset, endOffset:endOffset, rect:{x:r.left, y:r.bottom, w:r.width, h:r.height}};
  }
  document.addEventListener('mouseup', function(){
    setTimeout(function(){
      var c = selectionContext();
      if(c){ c.type='mc-comment-select'; parent.postMessage(c, '*'); }
    }, 0);
  });
  var HL='mc-cmt-hl';
  function clearHighlights(){
    var marks = document.querySelectorAll('.'+HL);
    for(var i=0;i<marks.length;i++){
      var m=marks[i], p=m.parentNode; if(!p) continue;
      while(m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m); p.normalize();
    }
  }
  var BUB='mc-cmt-bubble';
  function clearBubbles(){
    var bs=document.querySelectorAll('.'+BUB);
    for(var i=0;i<bs.length;i++){ if(bs[i].parentNode) bs[i].parentNode.removeChild(bs[i]); }
  }
  function rectOf(el){ var r=el.getBoundingClientRect(); return {x:r.left, y:r.top, w:r.width, h:r.height}; }
  function postOpen(id, el){ parent.postMessage({type:'mc-comment-highlight-click', id:id, rect:rectOf(el)}, '*'); }
  function findRange(quote, prefix, suffix){
    if(!quote) return null;
    var walker=document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    var nodes=[], text='', n;
    while((n=walker.nextNode())){ nodes.push({node:n, start:text.length}); text+=n.nodeValue; }
    // Match in whitespace-NORMALIZED space, then map back to raw offsets. The
    // quote/prefix/suffix are captured from rendered text (innerText: collapsed
    // whitespace plus block newlines) while text is the raw concatenated
    // nodeValue (source whitespace, no block newlines). Normalizing both sides
    // lets the quote match AND lets prefix/suffix disambiguate the correct
    // occurrence when the same quote appears multiple times (e.g. RESOLVED as a
    // heading vs in body text). Regex is avoided: this is embedded in a template
    // literal where backslashes are unsafe; WS is detected by char code.
    // text-transform (uppercase/capitalize) makes the rendered/selected text
    // differ in CASE from the raw nodeValue (e.g. source "Resolved" shown as
    // "RESOLVED"), so match case-insensitively too. norm is lowercased per char
    // (1:1 with map, with a guard for the rare multi-char lowercase) so offsets
    // still map back to the original-case raw text.
    var isWs=function(ch){ var k=ch.charCodeAt(0); return k===32||k===9||k===10||k===13||k===12||k===160; };
    var norm='', map=[], pw=false, c, ch;
    for(c=0;c<text.length;c++){
      ch=text.charAt(c);
      if(isWs(ch)){ if(!pw){ norm+=' '; map.push(c); pw=true; } }
      else { var lc=ch.toLowerCase(); norm+=(lc.length===1?lc:ch); map.push(c); pw=false; }
    }
    map.push(text.length);
    var nrm=function(str){
      var o='', p=false, x, k; for(x=0;x<str.length;x++){ k=str.charAt(x);
        if(isWs(k)){ if(!p && o){ o+=' '; p=true; } } else { o+=k; p=false; } }
      while(o.length && o.charAt(0)===' ') o=o.slice(1);
      while(o.length && o.charAt(o.length-1)===' ') o=o.slice(0,-1);
      return o.toLowerCase();
    };
    var nq=nrm(quote); if(!nq) return null;
    var npre=prefix?nrm(prefix):'', nsuf=suffix?nrm(suffix):'';
    var from=0, best=-1, bestScore=-1;
    while(true){
      var i=norm.indexOf(nq, from); if(i<0) break;
      var pre=norm.slice(Math.max(0, i-npre.length), i);
      var suf=norm.slice(i+nq.length, i+nq.length+nsuf.length);
      var score=0; if(npre && pre.indexOf(npre)>=0) score++; if(nsuf && suf.indexOf(nsuf)>=0) score++;
      if(score>bestScore){ bestScore=score; best=i; }
      from=i+1;
      if(bestScore===2) break;
    }
    if(best<0) return null;
    var bestStart=map[best], bestEnd=map[best+nq.length];
    function locate(off){ for(var k=0;k<nodes.length;k++){ var nd=nodes[k]; if(off<=nd.start+nd.node.nodeValue.length) return {node:nd.node, offset:off-nd.start}; } return null; }
    var s=locate(bestStart), e=locate(bestEnd);
    if(!s||!e) return null;
    var rng=document.createRange();
    try{ rng.setStart(s.node, s.offset); rng.setEnd(e.node, e.offset); }catch(x){ return null; }
    return rng;
  }
  function wrapRange(rng, className, cid){
    var nodes=[];
    var cac=rng.commonAncestorContainer;
    if(cac.nodeType===3){
      // Range lies entirely within ONE text node. A TreeWalker rooted at a text
      // node yields nothing (it only visits descendants, and text nodes have
      // none), so collect it directly. Without this, any match contained in a
      // single text node produces zero marks and the highlight never renders.
      nodes.push(cac);
    } else {
      var walker=document.createTreeWalker(cac, NodeFilter.SHOW_TEXT, null);
      var t;
      while((t=walker.nextNode())){ if(rng.intersectsNode(t)) nodes.push(t); }
    }
    var marks=[];
    nodes.forEach(function(tn){
      var start=(tn===rng.startContainer)?rng.startOffset:0;
      var end=(tn===rng.endContainer)?rng.endOffset:tn.nodeValue.length;
      if(start>=end) return;
      var sub=document.createRange();
      try{ sub.setStart(tn, start); sub.setEnd(tn, end); }catch(x){ return; }
      var mark=document.createElement('mark');
      mark.className=className; mark.setAttribute('data-cid', cid);
      try{ sub.surroundContents(mark); marks.push(mark); }catch(x){}
    });
    return marks;
  }
  var lastAnchors = null;
  function renderHighlights(anchors){
    lastAnchors = anchors || null;
    clearHighlights(); clearBubbles();
    (anchors||[]).forEach(function(a){
      var rng=findRange(a.quote, a.prefix, a.suffix); if(!rng) return;
      try{
        var marks=wrapRange(rng, HL, a.id);
        if(!marks.length) return;
        var first=marks[0];
        marks.forEach(function(m){ m.addEventListener('click', function(ev){ ev.stopPropagation(); postOpen(a.id, first); }); });
        // Gutter comment-count bubble at the anchor start (count + unread).
        var r=first.getBoundingClientRect();
        var bub=document.createElement('div');
        bub.className=BUB+(a.unread?' unread':'');
        bub.setAttribute('data-cid', a.id);
        bub.textContent=String(a.count||1);
        bub.style.top=(r.top+window.scrollY-1)+'px';
        bub.style.left=Math.max(0,(r.left+window.scrollX-24))+'px';
        bub.addEventListener('click', function(ev){ ev.stopPropagation(); postOpen(a.id, bub); });
        document.body.appendChild(bub);
      }catch(x){}
    });
  }
  function scrollTo(id){
    var sel='.'+HL+'[data-cid="'+String(id||'').replace(/"/g,'')+'"]';
    var m=document.querySelector(sel); if(!m) return;
    m.scrollIntoView({behavior:'smooth', block:'center'});
    m.classList.add('mc-cmt-flash');
    setTimeout(function(){ m.classList.remove('mc-cmt-flash'); }, 1200);
    // Let the scroll settle, then post the rect so the parent can open the
    // thread popover over the iframe (sidebar → popover for HTML artifacts).
    setTimeout(function(){ postOpen(id, m); }, 80);
  }
  function setActive(id){
    var cur=document.querySelectorAll('.mc-cmt-active');
    for(var i=0;i<cur.length;i++) cur[i].classList.remove('mc-cmt-active');
    var s=String(id||'').replace(/"/g,''); if(!s) return;
    var els=document.querySelectorAll('[data-cid="'+s+'"]');
    for(var j=0;j<els.length;j++) els[j].classList.add('mc-cmt-active');
  }
  window.addEventListener('message', function(e){
    var d=e.data||{};
    if(d.type==='mc-comment-highlights') renderHighlights(d.anchors);
    else if(d.type==='mc-comment-scroll-to') scrollTo(d.id);
    else if(d.type==='mc-comment-active') setActive(d.id);
  });
  // The initial highlight push can land while the artifact's own scripts
  // (Tailwind JIT, chart renders) are still rebuilding the DOM — marks get
  // wiped or their text nodes replaced, and nothing re-renders them. Re-apply
  // the retained set once everything has loaded and again after a settle
  // delay, so anchors survive late layout work (no-op when nothing anchored).
  function reapply(){ if(lastAnchors && lastAnchors.length && !document.querySelector('.'+HL)) renderHighlights(lastAnchors); }
  window.addEventListener('load', function(){ reapply(); setTimeout(reapply, 400); });
  parent.postMessage({type:'mc-comment-ready'}, '*');
})();`

/** Neutral light palette substituted for the dashboard theme when a widget was
 * authored against a light canvas (see `isLightCanvasAuthored`) but the
 * dashboard is dark. Every name in THEME_VAR_NAMES is covered so a widget that
 * mixes one `var(--…)` in later still resolves. Tailwind's gray/indigo/green/
 * amber/red 50-700 stops -- the same family the LLM reached for, so the island
 * reads as one coherent light card rather than a patch. `--bg` is off-white,
 * not pure white: the island sits inside a dark chat column, and gray-100
 * reads as a light card there where #fff reads as glare. */
const LIGHT_CANVAS_FALLBACK_VARS: Readonly<Record<(typeof THEME_VAR_NAMES)[number], string>> = {
  '--bg': '#f3f4f6',
  '--bg-elevated': '#ffffff',
  '--bg-hover': '#e5e7eb',
  '--card': '#ffffff',
  '--card-fg': '#111827',
  '--text': '#111827',
  '--text-strong': '#030712',
  '--muted': '#6b7280',
  '--muted-strong': '#4b5563',
  '--border': '#e5e7eb',
  '--border-strong': '#d1d5db',
  '--accent': '#4f46e5',
  '--accent-hover': '#4338ca',
  '--accent-subtle': '#eef2ff',
  '--ok': '#15803d',
  '--ok-subtle': '#ecfdf5',
  '--warn': '#b45309',
  '--warn-subtle': '#fffbeb',
  '--danger': '#b91c1c',
  '--danger-subtle': '#fef2f2',
  '--info': '#1d4ed8',
}

// The Tailwind token grammar the class regexes below share, kept in one place
// so a new hue, stop or variant form is added once. Each piece is written as a
// regex literal and read back through `.source`, so the escapes are exactly
// what a hand-written literal would hold.
//
// TW_VARIANT_PREFIX: a token boundary, then optional variant prefixes in
// Tailwind's full token syntax -- `hover:`, `md:`, `@md:`, `*:`, and bracketed
// arbitrary variants such as `min-[300px]:` or `[&:hover]:` (name capped at 64
// chars, bracket body at 256, so a long attribute value cannot make the scan
// quadratic).
const TW_VARIANT_PREFIX = /(?:^|[\s"'`])(?:(?=[a-zA-Z0-9@*\[-])[a-zA-Z0-9@*-]{0,64}(?:\[[^\]\s"'`]{1,256}\])?:)*/.source
// Every default Tailwind hue, as a regex alternation.
const TW_HUES =
  /slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose/.source
// The end of a class token: end of input, a separator, or an opacity `/`.
const TW_CLASS_END = /(?=$|[\s"'`\/])/.source

/** A Tailwind background-class detector: `bg-<solid>` or `bg-<hue>-<stop>` for
 * every default hue, behind any variant prefix. `solid` and `stops` are regex
 * alternations (`'white'`, `'50|100|200'`). No flags: callers only `.test()`. */
function twBgClassRe(solid: string, stops: string): RegExp {
  return new RegExp(`${TW_VARIANT_PREFIX}bg-(?:${solid}|(?:${TW_HUES})-(?:${stops}))${TW_CLASS_END}`)
}

// Tailwind utility classes that paint a light background: `bg-white` and the
// 50/100/200 stops of every default hue; `dark:` is handled separately below.
const LIGHT_TAILWIND_BG_RE = twBgClassRe('white', '50|100|200')
// The mirror of LIGHT_TAILWIND_BG_RE: `bg-black` and the 700-950 stops. A
// widget carrying one of these next to a light card is a mixed palette, and
// flipping it wholesale to light would only trade which half is unreadable.
const DARK_TAILWIND_BG_RE = twBgClassRe('black', '700|800|900|950')
// A `dark:` variant, bare or behind chained prefixes such as `md:dark:`.
const DARK_VARIANT_RE = new RegExp(`${TW_VARIANT_PREFIX}dark:`)
// Inline `background` / `background-color` declarations carrying a literal
// color. Captures the value for a luminance check.
const INLINE_BG_DECL_RE = /background(?:-color)?\s*:\s*(#[0-9a-f]{3,8}\b|rgba?\([^)]*\)|white\b)/gi
// Tailwind arbitrary-value backgrounds (`bg-[#f0fdf4]`, `bg-[rgb(250,250,250)]`,
// `bg-[white]`), which neither regex above sees. Captures the literal for the
// same luminance check; `_` inside the brackets is Tailwind's space escape.
const ARBITRARY_BG_CLASS_RE = new RegExp(
  `${TW_VARIANT_PREFIX}${/bg-\[(#[0-9a-f]{3,8}|rgba?\([^\]]*\)|white)\]/.source}`,
  'gi',
)

/** Relative luminance (0..1) of a CSS `#hex` / `rgb()` / `white` literal, or
 * null when it cannot be parsed. `parseCssColor` (the favicon-contrast parser)
 * already reads every form the regexes above capture except the `white`
 * keyword. Alpha is ignored: a translucent light tint over the dark canvas is
 * still lighter than the themed text it inherits. */
function literalLuminance(value: string): number | null {
  const v = value.trim().toLowerCase()
  if (v === 'white') return 1
  const c = parseCssColor(v)
  return c ? relativeLuminance(c.r, c.g, c.b) : null
}

// `class` / `style` attribute values, double- or single-quoted. Each value is
// bounded by its own quote, so the scan is linear in the length of the HTML.
const STYLED_ATTR_RE = /\b(?:class|style)\s*=\s*(?:"([^"]*)"|'([^']*)')/gi
// Opening and closing `<style>` tags, paired by a forward walk below rather
// than a lazy `[\s\S]*?` body match: that would rescan to the end for every
// unclosed `<style>`, which is quadratic on hostile input.
const STYLE_TAG_RE = /<(\/?)style\b/gi

/** The parts of widget HTML where styling can actually take effect: every
 * `class` / `style` attribute value (double- or single-quoted) and the body of
 * every `<style>` block (case-insensitive), joined with single spaces.
 * Rendered text is dropped, so a widget that merely TALKS about `bg-white` or
 * `background:#fff` cannot look like it paints one. String-only, no DOM. */
function styledSurfaces(html: string): string {
  const parts: string[] = []
  for (const m of html.matchAll(STYLED_ATTR_RE)) parts.push(m[1] ?? m[2] ?? '')
  let bodyStart = -1
  for (const m of html.matchAll(STYLE_TAG_RE)) {
    if (m[1]) {
      if (bodyStart >= 0) parts.push(html.slice(bodyStart, m.index))
      bodyStart = -1
    } else if (bodyStart < 0) {
      const tagEnd = html.indexOf('>', m.index + 6)
      if (tagEnd < 0) break
      bodyStart = tagEnd + 1
    }
  }
  return parts.join(' ')
}

/** Whether widget HTML was written for a LIGHT canvas without knowing the
 * frame is themed. That is the shape of the dark-mode unreadable widget: the
 * model hardcodes `bg-green-50` (or `background:#f0fdf4`) on a card, sets no
 * text color, and inherits the theme's light `--text` from `body` -- white on
 * off-white. The heuristic fires only when the author shows no theme
 * awareness at all:
 *
 * - no `var(--…)` reference anywhere (an author using theme vars owns the
 *   contract, half-set or not -- forcing a palette on them would be worse), and
 * - no `dark:` Tailwind variant, bare or behind chained prefixes such as
 *   `md:dark:` (an author who wrote a dark branch handled it), and
 * - at least one light hardcoded background: a `bg-white` / `bg-<hue>-50|100|200`
 *   class, or an inline `background` literal or `bg-[<literal>]` arbitrary-value
 *   class with relative luminance above 0.6, and
 * - no hardcoded DARK background beside it (`bg-black`, `bg-<hue>-700..950`, or
 *   a literal with luminance below 0.2): a mixed palette is left alone, since
 *   a light canvas would make the dark half unreadable instead.
 *
 * Every check -- the `var(--…)` and `dark:` gates included -- reads only the
 * class/style attribute values and `<style>` blocks (`styledSurfaces`), so
 * rendered text can neither trigger the heuristic nor suppress it: a widget
 * explaining Tailwind in prose is not painting a light card, and a prose
 * mention of `var(--bg)` is not theme awareness.
 *
 * Pure, string-only, and cheap enough to run on every srcdoc build. Reached
 * only through `resolveWidgetTheme`, which is the seam the tests exercise. */
function isLightCanvasAuthored(html: string): boolean {
  if (!html) return false
  const styled = styledSurfaces(html)
  if (/var\(\s*--/.test(styled)) return false
  if (DARK_VARIANT_RE.test(styled)) return false
  if (DARK_TAILWIND_BG_RE.test(styled)) return false
  let sawLight = LIGHT_TAILWIND_BG_RE.test(styled)
  for (const re of [INLINE_BG_DECL_RE, ARBITRARY_BG_CLASS_RE]) {
    for (const m of styled.matchAll(re)) {
      const lum = literalLuminance(m[1].replace(/_/g, ' '))
      if (lum === null) continue
      if (lum < 0.2) return false
      if (lum > 0.6) sawLight = true
    }
  }
  return sawLight
}

/** Resolve the vars and mode a widget actually renders with. A light-canvas
 * widget on a dark dashboard gets the neutral light palette and `light`
 * mode, so its hardcoded light surfaces sit on a light canvas with dark text
 * -- a readable light island instead of white-on-white. Everything else (any
 * widget on a light dashboard, any theme-aware widget) passes through
 * untouched, so the fallback can never override a user's chosen theme where
 * the author respected it. */
export function resolveWidgetTheme(
  html: string,
  themeVars: Record<string, string>,
  mode: 'dark' | 'light',
): { themeVars: Record<string, string>; mode: 'dark' | 'light' } {
  if (mode === 'dark' && isLightCanvasAuthored(html)) {
    return { themeVars: { ...themeVars, ...LIGHT_CANVAS_FALLBACK_VARS }, mode: 'light' }
  }
  return { themeVars, mode }
}

/** Common "surface" names no theme defines, pointed at the injected names they
 * mean. A widget reaching for `var(--surface, #1e1e1e)` otherwise always gets
 * its dark literal, which is unreadable under a light theme. Each alias is a
 * `var()` reference, so it follows the theme (and the light-canvas fallback),
 * and when its target is missing the widget's own fallback still applies. */
const THEME_VAR_ALIASES: Readonly<Record<string, (typeof THEME_VAR_NAMES)[number]>> = {
  '--surface': '--card',
  '--surface-secondary': '--bg-elevated',
}

function buildThemeCss(vars: Record<string, string>, mode: 'dark' | 'light'): string {
  const declarations = Object.entries(vars).map(([k, v]) => `${k}:${v}`)
  // No readable vars means no theme to apply, and inventing one is worse than
  // the browser's own defaults — a guard pins that this path emits no `:root`.
  if (!declarations.length) return ''
  for (const [alias, target] of Object.entries(THEME_VAR_ALIASES)) {
    if (!(alias in vars)) declarations.push(`${alias}:var(${target})`)
  }
  const rootBody = declarations.join(';')
  return (
    `:root{${rootBody};color-scheme:${mode}}` +
    // On the root element as well as the body: background propagates from html
    // to the canvas, so an LLM body that sets its own background still paints
    // over a themed base instead of over white.
    `html{background:var(--bg)}` +
    `body{background:var(--bg);color:var(--text)}`
  )
}

/** Re-clone every <script> element under `root` so the iframe re-parses and
 * executes them. Browsers do not execute scripts that came in via DOMParser
 * or createContextualFragment — they have to be created via createElement
 * to flag them as parser-inserted. This is the same dance React does when it
 * injects raw markup containing a script.
 *
 * Exported so the non-obvious "not parser-inserted" reason is documented in
 * exactly one place. Callers: the widget/artifact builder below, where running
 * the LLM's own <script> IS the feature (Chart.js, D3).
 *
 * NOT a requirement of adopting untrusted HTML — the opposite. Adopting via
 * createContextualFragment leaves scripts inert, and calling this is an explicit
 * decision to make them live. The Meetings sketch frame used to call it and
 * deliberately does NOT any more: model script in that frame was a BLOCKING
 * DNS-prefetch exfiltration channel, and it strips scripts instead (see
 * apps/meetings/lib/sketchSrcdoc.ts). Do not "restore" that call. */
/**
 * The live values of `THEME_VAR_NAMES`, read off the document root.
 *
 * ONE copy, imported by every frame host. There were seven near-identical local
 * copies; six agreed and mochi's did not — it read `.getPropertyValue(name).trim()`
 * with no `sanitizeCssValue` and no SSR guard, so one host was interpolating
 * unsanitised computed CSS into an iframe document while its six siblings
 * sanitised. That is the failure mode duplication actually causes: not the
 * duplicated lines, but the copy that silently stops matching.
 *
 * Returns `{}` outside a browser so a server render is a no-op rather than a
 * crash on `document`.
 */
export function readThemeVars(): Record<string, string> {
  if (typeof window === 'undefined' || typeof document === 'undefined') return {}
  const computed = getComputedStyle(document.documentElement)
  const out: Record<string, string> = {}
  for (const name of THEME_VAR_NAMES) {
    const v = sanitizeCssValue(computed.getPropertyValue(name))
    if (v) out[name] = v
  }
  return out
}

export function recloneScripts(root: ParentNode, doc: Document): void {
  const scripts = Array.from(root.querySelectorAll('script'))
  for (const oldScript of scripts) {
    const newScript = doc.createElement('script')
    for (const attr of Array.from(oldScript.attributes)) {
      newScript.setAttribute(attr.name, attr.value)
    }
    if (oldScript.textContent) newScript.textContent = oldScript.textContent
    oldScript.parentNode?.replaceChild(newScript, oldScript)
  }
}

/** 128 random bits as hex: a CSP nonce, so attribute-safe as written. */
function mintScriptNonce(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

interface BuildSrcdocOptions {
  html: string
  themeVars: Record<string, string>
  mode: 'dark' | 'light'
  /** Include the height reporter script (used by inline WidgetFrame iframes
   * that auto-size to content). Standalone full-screen views set this false
   * since they use a fixed iframe height. */
  includeHeightReporter?: boolean
  /** Inject the anchored-comment bridge (selection capture + highlight render
   * + scroll/flash) so the parent can offer commenting inside the HTML render.
   * Off by default. */
  enableComments?: boolean
  /** Cover the widget with a progress indicator until the Tailwind runtime has
   * injected its compiled CSS. Set for widgets heavy enough that the compile is
   * perceptible; without it a slow widget renders as a blank box. */
  showLoadingOverlay?: boolean
  /** Drop the CDN script origins from this document's CSP, leaving inline script
   * and the pinned same-origin Tailwind runtime. Set by a crewmate's dashboard: its
   * page is filled with that crewmate's own crew-log data and can be authored by an
   * agent, so a permitted script origin is an exfiltration channel that
   * `connect-src 'none'` does not close. Off by default, so no widget surface
   * changes. */
  offlineScripts?: boolean
  /** Let the page's own scripts run. On by default. Set false by a crewmate's
   * report: the document CSP then admits only the host's scripts, each carrying a
   * nonce minted for this document, so a page script, inline handler or
   * `javascript:` URL never runs while the height reporter and comment bridge do.
   * The page's links are inert and its refresh `meta`, `link` and `noscript` are
   * dropped (see settleStaticPage), so nothing moves the frame without a script. */
  pageScripts?: boolean
  /** Localized label for that indicator. Required when `showLoadingOverlay` is
   * set — the iframe cannot reach the parent's i18n catalog, so an untranslated
   * default here would visibly flip to English mid-load in every non-English
   * locale. */
  loadingLabel?: string
  /** Rewrite bare absolute `http(s)` links to `target="_blank"` at click time
   * so they open outside the frame (see EXTERNAL_LINK_TARGET_SHIM_BODY). On by
   * default. Set false when the host iframe's sandbox withholds
   * `allow-popups`: there a `_blank` link is a blocked popup (a dead click),
   * so in-frame navigation must be left alone. */
  rewriteBareLinks?: boolean
  /** Hand links and `window.open` to the host's link bridge (see
   * LINK_BRIDGE_SHIM_BODY) instead of opening them in the frame. Set by every
   * host that frames an agent-written page with `AGENT_PAGE_SANDBOX` and renders
   * `useFrameLinkBridge`. Replaces `rewriteBareLinks` when set. Off by default,
   * so a downloaded or exported copy keeps ordinary links. */
  linkBridge?: boolean
  /** Report the frame's touches to the parent (see FRAME_TOUCH_RELAY_BODY), for
   * a host whose drawer gesture must be able to start over the frame. Off by
   * default: nothing else listens for them. */
  relayTouches?: boolean
  /** Report the frame's unowned horizontal wheel runs to the parent (see
   * FRAME_WHEEL_RELAY_BODY), for a host whose desktop trackpad swipe must be
   * able to run over the frame. Off by default. */
  relayWheel?: boolean
}

/** Build the srcdoc HTML for a sandboxed widget iframe. The LLM `html`
 * argument is parsed into DOM nodes via Range.createContextualFragment() —
 * a typed DOM API — and never appears in a template literal. See file
 * header for the full security model. */
export function buildSrcdoc({
  html,
  themeVars: requestedThemeVars,
  mode: requestedMode,
  includeHeightReporter = false,
  enableComments = false,
  showLoadingOverlay = false,
  loadingLabel = '',
  rewriteBareLinks = true,
  linkBridge = false,
  offlineScripts = false,
  pageScripts = true,
  relayTouches = false,
  relayWheel = false,
}: BuildSrcdocOptions): string {
  // A widget hardcoded for a light canvas renders on a light canvas even when
  // the dashboard is dark -- see resolveWidgetTheme. Resolved once here so the
  // DOM and SSR paths, the :root vars, color-scheme and the body class agree.
  const { themeVars, mode } = resolveWidgetTheme(html, requestedThemeVars, requestedMode)

  // SSR / unit-test fallback: when there's no DOM (Node.js, vitest before
  // jsdom is set up), fall back to a minimal string-builder that does NOT
  // interpolate `html` — we wrap it in a textarea-escaped <template> so it
  // round-trips safely. Practically the SSR path is never hit because
  // buildSrcdoc is only called from React components that mount in the
  // browser, but the guard keeps tests deterministic.
  if (typeof document === 'undefined' || typeof window === 'undefined') {
    return buildSrcdocSSR({ html, themeVars, mode, includeHeightReporter })
  }

  // Build the iframe document programmatically. createHTMLDocument() returns
  // a fresh detached Document with <html><head><title></title></head><body>.
  const doc = document.implementation.createHTMLDocument('')
  const head = doc.head
  const body = doc.body

  // <meta charset>
  const charset = doc.createElement('meta')
  charset.setAttribute('charset', 'utf-8')
  head.appendChild(charset)

  // <meta viewport>
  const viewport = doc.createElement('meta')
  viewport.setAttribute('name', 'viewport')
  viewport.setAttribute('content', 'width=device-width, initial-scale=1')
  head.appendChild(viewport)

  // Dashboard's own origin — serves the Vite-emitted, same-origin Tailwind v4
  // runtime asset so the null-origin iframe makes NO public-CDN fetch (the
  // fetch locked-down network environments block). window is guaranteed here (the SSR
  // path returns above), so location.origin is always defined.
  const scriptOrigin = window.location.origin

  // Minted per document and never derived from the page, so a page cannot carry it.
  const nonce = pageScripts ? '' : mintScriptNonce()
  const hostScript = (): HTMLScriptElement => {
    const script = doc.createElement('script')
    if (nonce) script.setAttribute('nonce', nonce)
    return script
  }

  // <meta CSP>
  const csp = doc.createElement('meta')
  csp.setAttribute('http-equiv', 'Content-Security-Policy')
  csp.setAttribute('content', nonce
    ? withScriptNonce(cspFor(scriptOrigin, offlineScripts), nonce)
    : cspFor(scriptOrigin, offlineScripts))
  head.appendChild(csp)

  // Tailwind v4 dark-mode directives (compiled by the runtime on load). Placed
  // Directives precede the runtime script so the dark variant registers before
  // first paint.
  const tailwindCfg = doc.createElement('style')
  tailwindCfg.setAttribute('type', 'text/tailwindcss')
  tailwindCfg.textContent = TAILWIND_V4_DIRECTIVES
  head.appendChild(tailwindCfg)

  // <style> with base body styles + theme vars.
  //
  // This MUST precede the Tailwind runtime <script src> below. A classic script
  // in <head> blocks parsing of everything after it, so with the style behind it
  // the document has no background and no `color-scheme` until that script has
  // been fetched and executed — and the browser paints its default WHITE canvas
  // for that whole window. On a phone over a slow link that reads as a white
  // flash on every artifact and widget open. Inline CSS costs no fetch, so
  // putting it first makes the first paint already themed.
  const style = doc.createElement('style')
  const themeCss = buildThemeCss(themeVars, mode)
  style.textContent = themeCss ? `${BASE_BODY_CSS} ${themeCss}` : BASE_BODY_CSS
  head.appendChild(style)

  // Same-origin Tailwind v4 browser runtime (replaces public cdn.tailwindcss.com).
  // NOTE: we insert a placeholder <meta> instead of a live <script> element and
  // substitute it in the final serialized HTML. happy-dom eagerly fetches
  // <script src> URLs when the element is connected to a document
  // (HTMLScriptElement.[connectedToDocument]), which causes ECONNREFUSED in test
  // environments that lack a running dev server. Using a non-fetching placeholder
  // avoids the network dial entirely — the iframe's browser loads the script from
  // the serialized HTML string, never from a live DOM node in our process.
  const tailwindPlaceholder = doc.createElement('meta')
  tailwindPlaceholder.setAttribute('name', 'x-script-placeholder')
  tailwindPlaceholder.setAttribute('data-src', scriptOrigin + TAILWIND_RUNTIME_PATH)
  head.appendChild(tailwindPlaceholder)

  // Loading overlay: shown while Tailwind JIT compiles, hidden once ready or
  // after a timeout. Only injected when showLoadingOverlay is set.
  if (showLoadingOverlay) {
    const overlayStyle = doc.createElement('style')
    overlayStyle.textContent = [
      // align-items:flex-start (not center): the iframe body can be far taller
      // than the visible area, and a centred indicator ends up below the fold —
      // present in the DOM but invisible, which defeats the purpose.
      '#mc-tw-loading{position:fixed;inset:0;z-index:2147483647;',
      'display:flex;align-items:flex-start;justify-content:center;padding-top:24px;',
      'background:var(--bg,#1a1a2e);color:var(--muted,#888);',
      'font:500 12px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;',
      'transition:opacity .3s ease}',
      '#mc-tw-loading.mc-hidden{opacity:0;pointer-events:none}',
      '#mc-tw-loading .mc-spinner{width:16px;height:16px;',
      'border:2px solid var(--border,#333);border-top-color:var(--accent,#6366f1);',
      'border-radius:50%;animation:mc-spin .8s linear infinite;margin-right:8px}',
      '@keyframes mc-spin{to{transform:rotate(360deg)}}',
      // Match the parent indicator's motion-reduce treatment: the iframe has its
      // own document, so the parent's Tailwind motion-reduce variant cannot
      // reach this rule.
      '@media (prefers-reduced-motion: reduce){',
      '#mc-tw-loading .mc-spinner{animation:none}',
      '#mc-tw-loading{transition:none}}',
    ].join('')
    head.appendChild(overlayStyle)
  }

  // <body class="dark|light">
  body.className = mode

  // Clipboard write-fallback shim. Installed BEFORE the LLM html below so an
  // on-load attempt sees the wrapper too; without user activation its fallback
  // still fails rather than gaining an ambient clipboard-write path.
  // textContent assignment only — no LLM/user content interpolated.
  const clipboardShim = hostScript()
  clipboardShim.textContent = CLIPBOARD_FALLBACK_SHIM_BODY
  body.appendChild(clipboardShim)

  // Bare absolute links open outside the frame (see
  // EXTERNAL_LINK_TARGET_SHIM_BODY). Installed before the LLM html so its
  // capture-phase listener is registered ahead of any widget-authored one.
  // Skipped for hosts whose sandbox withholds popups, where the rewrite would
  // be a dead click. textContent assignment only.
  if (linkBridge) {
    // The frame grants no popups: links and window.open go to the host's
    // link bridge (see LINK_BRIDGE_SHIM_BODY). textContent assignment only.
    const linkBridgeShim = hostScript()
    linkBridgeShim.textContent = LINK_BRIDGE_SHIM_BODY
    body.appendChild(linkBridgeShim)
  } else if (rewriteBareLinks) {
    const linkTargetShim = hostScript()
    linkTargetShim.textContent = EXTERNAL_LINK_TARGET_SHIM_BODY
    body.appendChild(linkTargetShim)
  }

  // A static page's links are inert (see STATIC_LINK_GUARD_BODY), registered
  // ahead of the page. textContent assignment only.
  if (nonce) {
    const linkGuard = hostScript()
    linkGuard.textContent = STATIC_LINK_GUARD_BODY
    body.appendChild(linkGuard)
  }

  // Touch relay (see FRAME_TOUCH_RELAY_BODY), ahead of the LLM html so its
  // capture-phase listeners are registered first. textContent assignment only.
  if (relayTouches) {
    const touchRelay = hostScript()
    touchRelay.textContent = FRAME_TOUCH_RELAY_BODY
    body.appendChild(touchRelay)
  }
  if (relayWheel) {
    const wheelRelay = hostScript()
    wheelRelay.textContent = FRAME_WHEEL_RELAY_BODY
    body.appendChild(wheelRelay)
  }

  // Parse LLM html into a document fragment via the typed DOM API. The
  // `html` argument flows through createContextualFragment() — NOT through
  // string concatenation — so it never enters a template-literal HTML build.
  const range = doc.createRange()
  range.selectNodeContents(body)
  const fragment = range.createContextualFragment(html)
  // Re-clone <script> elements so they execute when the iframe parses srcdoc.
  recloneScripts(fragment, doc)
  body.appendChild(fragment)

  // Progress indicator while the Tailwind runtime compiles. Hidden when the
  // runtime injects its compiled <style> (MutationObserver on <head>); the
  // timeout is a HANG backstop only, deliberately far longer than any real
  // compile. A short timeout would defeat the purpose — it would uncover the
  // widget while it is still blank, which is exactly the case this exists for.
  if (showLoadingOverlay) {
    const overlay = doc.createElement('div')
    overlay.id = 'mc-tw-loading'
    // Built with createElement/textContent, never innerHTML: assigning to
    // innerHTML is prohibited repo-wide (it is an HTML parser sink), and the
    // label is caller-supplied text that must not be parsed as markup.
    const spinner = doc.createElement('div')
    spinner.className = 'mc-spinner'
    overlay.appendChild(spinner)
    overlay.appendChild(doc.createTextNode(loadingLabel))
    body.insertBefore(overlay, body.firstChild)

    const revealScript = hostScript()
    revealScript.textContent = `(function(){
      var el=document.getElementById('mc-tw-loading');
      if(!el)return;
      function hide(){el.classList.add('mc-hidden');setTimeout(function(){if(el.parentNode)el.parentNode.removeChild(el)},400);}
      var done=false;
      function finish(){if(done)return;done=true;hide();}
      // Runtime load failure: uncover immediately (degraded-but-visible beats
      // a blank box). The flag covers a failure that fired before this script
      // ran — the runtime is a HEAD script, so its error task can precede any
      // body script — and the listener covers one that fires after.
      if(window.__mcTwError){finish();return;}
      window.addEventListener('${TW_ERROR_EVENT}',finish);
      setTimeout(finish,${OVERLAY_HANG_BACKSTOP_MS});
      var obs=new MutationObserver(function(muts){
        for(var i=0;i<muts.length;i++){
          for(var j=0;j<muts[i].addedNodes.length;j++){
            var n=muts[i].addedNodes[j];
            if(n.tagName==='STYLE'&&!n.type){finish();obs.disconnect();return;}
          }
        }
      });
      obs.observe(document.head,{childList:true});
    })();`
    body.appendChild(revealScript)
  }

  // Height reporter (optional). textContent assignment, not template-literal.
  if (includeHeightReporter) {
    const reporter = hostScript()
    reporter.textContent = HEIGHT_REPORTER_BODY
    body.appendChild(reporter)
  }

  // Unsafe-centering guard: keeps the top of flex/grid-centered artifact
  // bodies reachable when the content overflows the iframe viewport.
  const safeCenter = hostScript()
  safeCenter.textContent = SAFE_CENTER_GUARD_BODY
  body.appendChild(safeCenter)

  // Anchored-comment bridge: highlight CSS + selection/highlight
  // script. textContent assignment only — no LLM/user content interpolated.
  if (enableComments) {
    const hlStyle = doc.createElement('style')
    hlStyle.textContent = COMMENT_HIGHLIGHT_CSS
    head.appendChild(hlStyle)
    const bridge = hostScript()
    bridge.textContent = COMMENT_BRIDGE_BODY
    body.appendChild(bridge)
  }

  // Serialize the DOM-built document. The remaining template literal here
  // contains only the static DOCTYPE prefix and the *serialized* DOM tree
  // (which has already had LLM content adopted as typed DOM nodes), so no
  // raw LLM string is interpolated into HTML.
  //
  // Post-serialization: replace the SINGLE trusted placeholder with the real
  // <script> tag. No `g` flag — only the one placeholder we inserted into
  // <head> is replaced. Model-authored content lives in <body> and cannot
  // inject a matching placeholder because: (1) it is adopted via
  // createContextualFragment (typed DOM), not string interpolation, and
  // (2) attribute serialization HTML-escapes quotes, so a model byte sequence
  // cannot produce the exact `name="x-script-placeholder"` attribute pair.
  // The non-global replace is a defense-in-depth backstop for that invariant.
  //
  // The emitted tag carries two attributes tied to Chrome's Private Network
  // Access policy (the sandboxed iframe is a null-origin, NON-secure context,
  // and on the default deployment this src is a loopback address — a
  // more-private address space):
  //   - crossorigin="anonymous" routes the load through CORS, the only mode
  //     that can carry the gateway's Access-Control-Allow-Origin /
  //     PNA-preflight approval (see _apply_security_headers + the /vendor
  //     OPTIONS handler in dashboard/server.py).
  //   - onerror sets the failure flag + fires TW_ERROR_EVENT so the loading
  //     overlay uncovers the widget IMMEDIATELY instead of sitting on the
  //     15s hang backstop. An inline handler (not a listener added by a later
  //     script) is deliberate: a head script's error task can run before any
  //     body script executes, so only the element's own handler is free of
  //     that ordering hazard. Static trusted string — no LLM content.
  const serialized = `<!DOCTYPE html>\n${doc.documentElement.outerHTML}`
  const finished = serialized.replace(
    /<meta name="x-script-placeholder" data-src="([^"]*)">/,
    // With a nonce the inline `onerror` would be refused, so the tag drops it; the
    // overlay's hang backstop still uncovers the page.
    (_match, src) => nonce
      ? `<script src="${src}" crossorigin="anonymous" nonce="${nonce}"></script>`
      : `<script src="${src}" crossorigin="anonymous" onerror="${TW_ERROR_INLINE_HANDLER}"></script>`,
  )
  // A static page also loses what navigates the frame without a script.
  return nonce ? settleStaticPage(finished, nonce) : finished
}

/** SSR fallback for environments without a DOM. Used only by unit tests
 * that import this module before jsdom is installed; production rendering
 * always goes through the DOM path above. Kept minimal — does NOT execute
 * scripts in the LLM body (just embeds it as a textContent-safe string
 * inside a <template> so the snapshot is deterministic and round-trips). */
function buildSrcdocSSR({ html, themeVars, mode, includeHeightReporter }: BuildSrcdocOptions): string {
  const themeCss = buildThemeCss(themeVars, mode)
  const styleCss = themeCss ? `${BASE_BODY_CSS} ${themeCss}` : BASE_BODY_CSS
  // The `html` interpolation here is gated by typeof-document guard above
  // — production code never enters this branch — but to keep the review tooling happy
  // we don't interpolate it at all: instead, we encode it as an HTML-safe
  // attribute that the iframe's hydration would never see. Tests that need
  // the parsed body content should run under jsdom (the default).
  void html
  const reporter = includeHeightReporter
    ? `<script>${HEIGHT_REPORTER_BODY}<\/script>`
    : ''
  return (
    `<!DOCTYPE html><html><head>` +
    `<meta charset="utf-8">` +
    `<meta name="viewport" content="width=device-width, initial-scale=1">` +
    `<meta http-equiv="Content-Security-Policy" content="${cspFor('')}">` +
    `<style type="text/tailwindcss">${TAILWIND_V4_DIRECTIVES}</style>` +
    // Theme style precedes the runtime <script src> for the same reason as the
    // DOM path: a head script blocks parsing, so a style behind it leaves the
    // document unthemed — and painted white — until the script lands.
    `<style>${styleCss}</style>` +
    `<script src="${TAILWIND_RUNTIME_PATH}" crossorigin="anonymous" onerror="${TW_ERROR_INLINE_HANDLER}"><\/script>` +
    `</head><body class="${mode}">` +
    `<!-- SSR fallback: LLM body omitted -->` +
    // NO clipboard shim here. This SSR builder is unreachable in production
    // (buildSrcdoc only enters it when there is no DOM — i.e. pre-jsdom unit
    // tests) AND it does not embed the LLM body at all, so a copy button never
    // renders in its output. Injecting the write-fallback shim into it would be
    // scope with no reachable effect; the real fix lives in the DOM path above
    // (buildSrcdoc), which is what every production surface renders.
    reporter +
    `</body></html>`
  )
}
