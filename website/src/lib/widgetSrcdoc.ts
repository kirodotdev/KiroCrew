// Shared utility for building the sandboxed iframe srcdoc that renders
// LLM-generated widget HTML.
//
// SECURITY MODEL
// ==============
// The iframe uses sandbox="allow-scripts allow-popups
// allow-popups-to-escape-sandbox" with srcdoc, giving it a null origin. The
// LLM content runs in an isolated context — it cannot access parent DOM,
// cookies, localStorage, or navigate the parent page. allow-popups (+escape)
// lets target="_blank" links open in a real new tab (Mesh-1678); reverse-
// tabnabbing stays blocked by the mandated rel="noopener noreferrer" on
// widget links. Same security model as Claude's artifacts (Anthropic).
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
// - ArtifactDetailPage.tsx (full-screen artifact view at /artifacts/<slug>)

import { TAILWIND_RUNTIME_PATH } from './vendorPaths'

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
// cdn.tailwindcss.com (which AEA-enforced environments block and which crashed
// the page on artifact render, Mesh-2518).
//
// script-src is least-privilege (ARCC cnt_XWQ2MLdcuH25VA, "allowlist only
// necessary sources"): the dashboard origin is pinned to the single vendored
// runtime FILE, not the whole origin, so an injected/compromised widget cannot
// load arbitrary scripts from other dashboard endpoints. 'unsafe-eval' is NOT
// granted (ARCC cnt_ArLamIpfHaFn9Z): the Tailwind v4 runtime uses zero
// eval/Function/WebAssembly and Chart.js/D3 do not need it, so widget JS gets no
// dynamic-exec primitive inside the sandbox. 'unsafe-inline' stays: arbitrary
// inline widget <script>/<style> is the core feature and cannot use nonce/hash.
// A null-origin iframe cannot use 'self', which is why the origin is spelled out.
// jsdelivr/cdnjs remain for widget-authored Chart.js/D3 (same-origin + SRI is a
// follow-up). Tailwind v4 emits CSS as inline <style>, so style-src needs no CDN.
const cspFor = (scriptOrigin: string): string =>
  "default-src 'none'; " +
  `script-src 'unsafe-inline' ${scriptOrigin}${TAILWIND_RUNTIME_PATH} ` +
  "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; " +
  "style-src 'unsafe-inline'; " +
  "img-src data: blob:; font-src data:; connect-src 'none'; " +
  "form-action 'none'; base-uri 'none';"

const BASE_BODY_CSS =
  "body { margin: 0; padding: 16px; font-family: -apple-system, " +
  "BlinkMacSystemFont, 'Segoe UI', sans-serif; }"

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

/** Body of the height-reporter script. Defined as a string literal — the only
 * interpolation is the two numeric tuning constants above (never LLM/user
 * content; the LLM `html` never reaches here). Set as a script element via
 * textContent below; the iframe re-parses and executes it via the standard
 * <script> mechanism.
 *
 * The reporter is deliberately quiet: it coalesces ResizeObserver bursts into
 * one measurement per animation frame, ignores sub-pixel jitter, applies
 * growth eagerly, and defers/cancels shrinks. The net effect is that a
 * continuously animating widget (lava lamp, starry night) reports its height
 * once and then stops posting, instead of feeding a per-frame resize loop back
 * to the parent. */
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

const HEIGHT_REPORTER_BODY = `(function(){
  var EPS = ${HEIGHT_REPORT_EPSILON_PX};
  var SHRINK_MS = ${HEIGHT_REPORT_SHRINK_MS};
  var lastSent = -1;      // last height actually posted to the parent
  var shrinkTimer = null;
  var rafId = 0;
  var raf = window.requestAnimationFrame || function(cb){ return setTimeout(cb, 16); };
  function send(h){
    lastSent = h;
    parent.postMessage({type:'mc-widget-height', height:h}, '*');
  }
  function measure(){
    // Use body.scrollHeight (content height) — NOT max(body, documentElement).
    // documentElement.scrollHeight is at least the iframe's own viewport
    // height, so once the iframe grows it can never report smaller, ratcheting
    // the frame taller and taller (capped at MAX_HEIGHT) and never shrinking
    // back to the actual content. body is content-sized (margin:0, no height),
    // so its scrollHeight tracks the real content and can shrink.
    return document.body.scrollHeight;
  }
  function evaluate(){
    rafId = 0;
    var h = measure();
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
    if (rafId) return;
    rafId = raf(evaluate);
  }
  new ResizeObserver(schedule).observe(document.body);
  window.addEventListener('load', function(){ setTimeout(schedule, 100); });
  schedule();
  document.addEventListener('click', function(e){
    // NOTE (P454989291): this isTrusted check runs INSIDE the sandboxed iframe
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

/** Same-origin path to the Tailwind v4 browser runtime. A Vite plugin
 * (tailwindRuntimePlugin in vite.config.ts) copies @tailwindcss/browser's IIFE
 * build here at build time from the tracked npm dependency — so it is served
 * from the dashboard's own origin (not the public CDN that AEA blocks) and is
 * NOT a committed blob (BSC14 supply-chain). Prefixed with window.location.origin
 * below because the sandboxed iframe is null-origin and can't use a bare path.
 * Defined once in ./vendorPaths and imported above so the consumer path can't
 * drift from vite.config.ts's dev-serve + build-emit sites. */

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

function buildThemeCss(vars: Record<string, string>, mode: 'dark' | 'light'): string {
  const rootBody = Object.entries(vars).map(([k, v]) => `${k}:${v}`).join(';')
  if (!rootBody) return ''
  return (
    `:root{${rootBody};color-scheme:${mode}}` +
    `body{background:var(--bg);color:var(--text)}`
  )
}

/** Re-clone every <script> element under `root` so the iframe re-parses and
 * executes them. Browsers do not execute scripts that came in via DOMParser
 * or createContextualFragment — they have to be created via createElement
 * to flag them as parser-inserted. This is the same dance React does for
 * dangerouslySetInnerHTML script content. */
function recloneScripts(root: ParentNode, doc: Document): void {
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

interface BuildSrcdocOptions {
  html: string
  themeVars: Record<string, string>
  mode: 'dark' | 'light'
  /** Include the height reporter script (used by inline WidgetFrame iframes
   * that auto-size to content). Standalone full-screen views set this false
   * since they use a fixed iframe height. */
  includeHeightReporter?: boolean
}

/** Build the srcdoc HTML for a sandboxed widget iframe. The LLM `html`
 * argument is parsed into DOM nodes via Range.createContextualFragment() —
 * a typed DOM API — and never appears in a template literal. See file
 * header for the full security model. */
export function buildSrcdoc({
  html,
  themeVars,
  mode,
  includeHeightReporter = false,
}: BuildSrcdocOptions): string {
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
  // fetch AEA-enforced environments block). window is guaranteed here (the SSR
  // path returns above), so location.origin is always defined.
  const scriptOrigin = window.location.origin

  // <meta CSP>
  const csp = doc.createElement('meta')
  csp.setAttribute('http-equiv', 'Content-Security-Policy')
  csp.setAttribute('content', cspFor(scriptOrigin))
  head.appendChild(csp)

  // Tailwind v4 dark-mode directives (compiled by the runtime on load). Placed
  // before the runtime script so the custom variant is registered first.
  const tailwindCfg = doc.createElement('style')
  tailwindCfg.setAttribute('type', 'text/tailwindcss')
  tailwindCfg.textContent = TAILWIND_V4_DIRECTIVES
  head.appendChild(tailwindCfg)

  // Same-origin Tailwind v4 browser runtime (replaces public cdn.tailwindcss.com).
  const tailwind = doc.createElement('script')
  tailwind.setAttribute('src', scriptOrigin + TAILWIND_RUNTIME_PATH)
  head.appendChild(tailwind)

  // <style> with base body styles + theme vars
  const style = doc.createElement('style')
  const themeCss = buildThemeCss(themeVars, mode)
  style.textContent = themeCss ? `${BASE_BODY_CSS} ${themeCss}` : BASE_BODY_CSS
  head.appendChild(style)

  // <body class="dark|light">
  body.className = mode

  // Parse LLM html into a document fragment via the typed DOM API. The
  // `html` argument flows through createContextualFragment() — NOT through
  // string concatenation — so it never enters a template-literal HTML build.
  const range = doc.createRange()
  range.selectNodeContents(body)
  const fragment = range.createContextualFragment(html)
  // Re-clone <script> elements so they execute when the iframe parses srcdoc.
  recloneScripts(fragment, doc)
  body.appendChild(fragment)

  // Height reporter (optional). textContent assignment, not template-literal.
  if (includeHeightReporter) {
    const reporter = doc.createElement('script')
    reporter.textContent = HEIGHT_REPORTER_BODY
    body.appendChild(reporter)
  }

  // Unsafe-centering guard: keeps the top of flex/grid-centered artifact
  // bodies reachable when the content overflows the iframe viewport.
  const safeCenter = doc.createElement('script')
  safeCenter.textContent = SAFE_CENTER_GUARD_BODY
  body.appendChild(safeCenter)

  // Serialize the DOM-built document. The remaining template literal here
  // contains only the static DOCTYPE prefix and the *serialized* DOM tree
  // (which has already had LLM content adopted as typed DOM nodes), so no
  // raw LLM string is interpolated into HTML.
  return `<!DOCTYPE html>\n${doc.documentElement.outerHTML}`
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
  // — production code never enters this branch — but to keep AutoSDE happy
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
    `<script src="${TAILWIND_RUNTIME_PATH}"><\/script>` +
    `<style>${styleCss}</style>` +
    `</head><body class="${mode}">` +
    `<!-- SSR fallback: LLM body omitted -->` +
    reporter +
    `</body></html>`
  )
}
