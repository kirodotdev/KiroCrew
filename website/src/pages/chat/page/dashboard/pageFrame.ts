/**
 * The document a dashboard's page is shown in (PoC): the conductor's HTML, the
 * person's theme as CSS variables, a CSP that shuts off the network, and a small
 * helper script that talks to the pane over postMessage.
 *
 * The frame is sandboxed with scripts only (an opaque origin), so the pane cannot
 * reach into it: the helper is how picking works. It reports what the pointer is
 * over and what was clicked (in Pick mode, where the page's own clicks are held),
 * the on-screen box of each element the pane tracks (asks, overlay anchors,
 * selection), and the keys pressed inside the page, so shortcuts work there too.
 */

/** Every message the helper sends carries `dw`, its kind. */
export type FrameMessage =
  | { dw: 'ready' }
  | { dw: 'hover'; target: string | null; rect: FrameRect | null; label: string }
  | { dw: 'pick'; target: string; label: string; shift: boolean }
  | { dw: 'rects'; rects: Record<string, FrameRect | null> }
  | { dw: 'key'; key: string; meta: boolean; ctrl: boolean; alt: boolean; shift: boolean }

export interface FrameRect { x: number; y: number; w: number; h: number }

export function isFrameMessage(v: unknown): v is FrameMessage {
  return !!v && typeof v === 'object' && typeof (v as { dw?: unknown }).dw === 'string'
}

const CDNS = 'https://cdn.jsdelivr.net https://unpkg.com https://cdnjs.cloudflare.com'
const CSP = [
  "default-src 'none'",
  `script-src 'unsafe-inline' ${CDNS}`,
  `style-src 'unsafe-inline' ${CDNS}`,
  'font-src data:',
  'img-src data: blob:',
  "connect-src 'none'",
  "frame-src 'none'",
  "form-action 'none'",
  "base-uri 'none'",
].join('; ')

// Runs inside the frame. Plain ES2017, no imports: it is inlined as text.
const HELPER = String.raw`(function(){
  var mode = 'pick', tracked = [], post = function(m){ parent.postMessage(m, '*') };
  function targetOf(el){
    var hit = el && el.closest ? el.closest('[data-id]') : null;
    if (hit) return 'el:' + hit.getAttribute('data-id');
    if (!el || el === document.body || el === document.documentElement) return null;
    var parts = [];
    for (var n = el; n && n.nodeType === 1 && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      var i = 1, s = n;
      while ((s = s.previousElementSibling)) i++;
      parts.unshift(n.tagName.toLowerCase() + ':nth-child(' + i + ')');
    }
    return 'css:body > ' + parts.join(' > ');
  }
  function resolve(t){
    try {
      if (t.indexOf('el:') === 0) return document.querySelector('[data-id="' + CSS.escape(t.slice(3)) + '"]');
      if (t.indexOf('css:') === 0) return document.querySelector(t.slice(4));
    } catch (e) {}
    return null;
  }
  function box(el){ if (!el) return null; var r = el.getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height } }
  function label(el){ var t = (el && (el.getAttribute('aria-label') || el.innerText || el.textContent)) || ''; return t.replace(/\s+/g, ' ').trim().slice(0, 80) }
  var hovered = null;
  function report(){
    var rects = {};
    for (var i = 0; i < tracked.length; i++) rects[tracked[i]] = box(resolve(tracked[i]));
    post({ dw: 'rects', rects: rects });
    if (hovered) post({ dw: 'hover', target: hovered, rect: box(resolve(hovered)), label: label(resolve(hovered)) });
  }
  var queued = false;
  function soon(){ if (queued) return; queued = true; requestAnimationFrame(function(){ queued = false; report() }) }
  addEventListener('message', function(e){
    var d = e.data || {};
    if (d.dw === 'mode') { mode = d.mode; document.documentElement.toggleAttribute('data-dw-pick', mode === 'pick'); if (mode !== 'pick') { hovered = null; post({ dw: 'hover', target: null, rect: null, label: '' }) } }
    if (d.dw === 'track') { tracked = d.targets || []; soon() }
  });
  document.addEventListener('pointermove', function(e){
    if (mode !== 'pick') return;
    var t = targetOf(e.target);
    if (t === hovered) return;
    hovered = t;
    post({ dw: 'hover', target: t, rect: t ? box(resolve(t)) : null, label: t ? label(resolve(t)) : '' });
  }, true);
  document.addEventListener('pointerleave', function(){ if (hovered) { hovered = null; post({ dw: 'hover', target: null, rect: null, label: '' }) } });
  // Holding the press also holds the focus move, so take focus here: keys pressed
  // after a pick (C to ask) then reach this page and are reported.
  function hold(e){ if (mode === 'pick') { e.preventDefault(); e.stopPropagation(); if (e.type === 'pointerdown') window.focus() } }
  ['pointerdown', 'mousedown', 'mouseup', 'pointerup', 'dblclick', 'auxclick', 'submit'].forEach(function(k){ document.addEventListener(k, hold, true) });
  document.addEventListener('click', function(e){
    if (mode !== 'pick') return;
    e.preventDefault(); e.stopPropagation();
    var t = targetOf(e.target);
    if (t) post({ dw: 'pick', target: t, label: label(resolve(t)), shift: e.shiftKey });
    else post({ dw: 'pick', target: '', label: '', shift: e.shiftKey });
  }, true);
  document.addEventListener('keydown', function(e){
    var a = document.activeElement, typing = a && (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
    if (typing && !e.metaKey && !e.ctrlKey) return;
    post({ dw: 'key', key: e.key, meta: e.metaKey, ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey });
    if (mode === 'pick' && !typing) e.preventDefault();
  }, true);
  addEventListener('scroll', soon, true);
  addEventListener('resize', soon);
  new MutationObserver(soon).observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
  setInterval(report, 1000);
  document.documentElement.toggleAttribute('data-dw-pick', true);
  post({ dw: 'ready' });
})();`

const PICK_CSS = 'html[data-dw-pick], html[data-dw-pick] * { cursor: default !important; user-select: none !important; }'

/** The full document for *html* (the conductor's page) under *themeVars*. */
export function buildPageDocument(html: string, themeVars: Record<string, string>): string {
  const vars = Object.entries(themeVars).map(([k, v]) => `${k}:${v}`).join(';')
  const base = `:root{${vars};color-scheme:light dark}html,body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font-sans,system-ui,sans-serif)}`
  const page = html.replace(/^\s*<!doctype[^>]*>/i, '')
  return `<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${CSP}"><style>${base}${PICK_CSS}</style><script>${HELPER}</script>${page}`
}
