// Shared utility for building the sandboxed iframe srcdoc that renders
// LLM-generated widget HTML.
//
// SECURITY MODEL
// ==============
// The iframe uses sandbox="allow-scripts allow-popups
// allow-popups-to-escape-sandbox" with srcdoc, giving it a null origin. The
// LLM content runs in an isolated context — it cannot access parent DOM,
// cookies, localStorage, or navigate the parent page. allow-popups (+escape)
// lets target="_blank" links open in a real new tab; reverse-
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

/** How long the loading indicator waits before uncovering the widget anyway.
 * This is a HANG backstop, not a compile budget: the indicator normally clears
 * the moment the Tailwind runtime injects its compiled <style>. Keeping it long
 * matters — a short value uncovers the widget while it is still unstyled, which
 * reproduces the blank-widget symptom the indicator exists to prevent. */
const OVERLAY_HANG_BACKSTOP_MS = 15000

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

/** Same-origin path to the Tailwind v4 browser runtime. A Vite plugin
 * (tailwindRuntimePlugin in vite.config.ts) copies @tailwindcss/browser's IIFE
 * build here at build time from the tracked npm dependency — so it is served
 * from the dashboard's own origin (not the public CDN that locked-down networks block) and is
 * NOT a committed blob (supply-chain guidance). Prefixed with window.location.origin
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

/** In-iframe comment bridge. Vanilla JS string — no LLM/user
 * interpolation (no `${}`), set via textContent and re-parsed by the iframe.
 * Brings anchored commenting + highlights into the sandboxed HTML render that
 * the parent cannot reach directly:
 *   - mouseup with a text selection -> postMessage 'mc-comment-select'
 *     {quote, prefix, suffix, rect} so the parent opens the comment popover;
 *   - 'mc-comment-highlights' {anchors:[{id,quote,prefix,suffix}]} -> wrap each
 *     matched run in <mark.mc-cmt-hl data-cid> (click -> 'mc-comment-highlight-click');
 *   - 'mc-comment-scroll-to' {id} -> scroll the mark into view + flash it;
 *   - posts 'mc-comment-ready' once so the parent pushes the initial set. */
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
  /** Localized label for that indicator. Required when `showLoadingOverlay` is
   * set — the iframe cannot reach the parent's i18n catalog, so an untranslated
   * default here would visibly flip to English mid-load in every non-English
   * locale. */
  loadingLabel?: string
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
  enableComments = false,
  showLoadingOverlay = false,
  loadingLabel = '',
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
  // fetch locked-down network environments block). window is guaranteed here (the SSR
  // path returns above), so location.origin is always defined.
  const scriptOrigin = window.location.origin

  // <meta CSP>
  const csp = doc.createElement('meta')
  csp.setAttribute('http-equiv', 'Content-Security-Policy')
  csp.setAttribute('content', cspFor(scriptOrigin))
  head.appendChild(csp)

  // Tailwind v4 dark-mode directives (compiled by the runtime on load). Placed
  // Directives precede the runtime script so the dark variant registers before
  // first paint.
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

    const revealScript = doc.createElement('script')
    revealScript.textContent = `(function(){
      var el=document.getElementById('mc-tw-loading');
      if(!el)return;
      function hide(){el.classList.add('mc-hidden');setTimeout(function(){if(el.parentNode)el.parentNode.removeChild(el)},400);}
      var done=false;
      function finish(){if(done)return;done=true;hide();}
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
    const reporter = doc.createElement('script')
    reporter.textContent = HEIGHT_REPORTER_BODY
    body.appendChild(reporter)
  }

  // Unsafe-centering guard: keeps the top of flex/grid-centered artifact
  // bodies reachable when the content overflows the iframe viewport.
  const safeCenter = doc.createElement('script')
  safeCenter.textContent = SAFE_CENTER_GUARD_BODY
  body.appendChild(safeCenter)

  // Anchored-comment bridge: highlight CSS + selection/highlight
  // script. textContent assignment only — no LLM/user content interpolated.
  if (enableComments) {
    const hlStyle = doc.createElement('style')
    hlStyle.textContent = COMMENT_HIGHLIGHT_CSS
    head.appendChild(hlStyle)
    const bridge = doc.createElement('script')
    bridge.textContent = COMMENT_BRIDGE_BODY
    body.appendChild(bridge)
  }

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
    `<script src="${TAILWIND_RUNTIME_PATH}"><\/script>` +
    `<style>${styleCss}</style>` +
    `</head><body class="${mode}">` +
    `<!-- SSR fallback: LLM body omitted -->` +
    reporter +
    `</body></html>`
  )
}
