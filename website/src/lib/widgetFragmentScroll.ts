// The in-iframe half of in-page link scrolling for a frame sized to its own
// document. widgetSrcdoc.ts injects FRAGMENT_SCROLL_BODY (via textContent)
// beside the height reporter, because only a self-sizing frame needs it.
//
// Such a frame is exactly as tall as its document, so its own viewport never
// scrolls: a click on `<a href="#section">` performs the fragment navigation and
// nothing on screen moves, because the page the reader is scrolling is the
// HOST's, and a cross-origin frame (the sandbox gives it an opaque origin) does
// not scroll its embedder. This script leaves the navigation to the browser,
// then measures where the target sits in the frame and posts
// `{type: 'mc-widget-scroll-to', top}` so the host can scroll its own container
// there (hooks/useFrameFragmentScroll.ts). A frame held at a fixed height
// scrolls itself as before, and its host ignores the message.
//
// Static trusted string: no LLM/user content and no interpolation.

/** Frame -> host: scroll to `top`, a CSS pixel offset from the frame's top edge. */
export const FRAGMENT_SCROLL_MESSAGE = 'mc-widget-scroll-to'

/**
 * - A plain left click on a same-document link (its href resolves to this
 *   document plus a fragment) whose effective target is this frame. A link with
 *   no `target` under a `<base target="_blank">` is set to `_self` during
 *   dispatch, so it scrolls instead of asking for a window the sandbox refuses.
 *   An explicit `target="_blank"` (or any other name) is left alone.
 * - Decided a task after dispatch, so a page that cancels the click (in-page
 *   tabs on `#tab` links) keeps that and nothing is posted.
 * - `hashchange` is reported too, which covers script that sets
 *   `location.hash` and Back/Forward between fragments. A change to an empty
 *   hash (Back to the entry before the first click) moves nothing.
 * - The target is the element with that id, else the first with that name;
 *   `#` and `#top` with no such element mean the top of the document. The
 *   target's `scroll-margin-top` is honoured, as the browser's own scroll does.
 */
export const FRAGMENT_SCROLL_BODY = `(function(){
  if (window.parent === window) return;
  var T = '${FRAGMENT_SCROLL_MESSAGE}';
  var P = window.parent;
  var XLINK = 'http://www.w3.org/1999/xlink';
  function here(){ return String(location.href).split('#')[0]; }
  function hrefOf(n){
    var h = n.getAttribute('href');
    if (h == null && typeof n.getAttributeNS === 'function') h = n.getAttributeNS(XLINK, 'href');
    return h;
  }
  function linkOf(e){
    var path = typeof e.composedPath === 'function' ? e.composedPath() : [e.target];
    for (var i = 0; i < path.length; i++) {
      var n = path[i];
      if (n && n.nodeType === 1 && (n.localName === 'a' || n.localName === 'area') && hrefOf(n) != null) return n;
    }
    return null;
  }
  // The fragment a same-document link points at ('#' for a bare '#'), else null.
  function hashOf(a){
    var u;
    try { u = new URL(hrefOf(a), document.baseURI); } catch (_) { return null; }
    if (u.href.indexOf('#') < 0 || u.href.split('#')[0] !== here()) return null;
    return u.hash || '#';
  }
  function find(hash){
    var id = hash.slice(1);
    try { id = decodeURIComponent(id); } catch (_) {}
    var el = id ? (document.getElementById(id) || document.getElementsByName(id)[0]) : null;
    if (el) return el;
    return (!id || id.toLowerCase() === 'top') ? document.documentElement : null;
  }
  function report(hash){
    var el = find(hash);
    if (!el) return;
    var top = el.getBoundingClientRect().top;
    var margin = parseFloat(getComputedStyle(el).scrollMarginTop);
    if (margin > 0) top -= margin;
    try { P.postMessage({ type: T, top: top }, '*'); } catch (_) {}
  }
  window.addEventListener('click', function(e){
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    var a = linkOf(e);
    if (!a || (typeof a.closest === 'function' && a.closest('[data-action]'))) return;
    var hash = hashOf(a);
    if (hash === null) return;
    var t = a.getAttribute('target');
    if (t === null) {
      var b = document.querySelector('base[target]');
      var bt = b ? (b.getAttribute('target') || '').trim().toLowerCase() : '';
      if (bt && bt !== '_self') a.setAttribute('target', '_self');
    } else {
      t = t.trim().toLowerCase();
      if (t && t !== '_self') return;
    }
    // A click that also changes the hash is reported again by hashchange. The
    // second report names the same spot, so the host's scroll is unchanged.
    setTimeout(function(){ if (!e.defaultPrevented) report(hash); }, 0);
  }, true);
  window.addEventListener('hashchange', function(){ if (location.hash) report(location.hash); });
})();`
