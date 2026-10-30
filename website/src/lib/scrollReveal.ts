/**
 * Marks a scroll box while it is being scrolled, so the stylesheet can show its
 * thumb only then.
 *
 * The dashboard paints no scrollbar at rest (index.css, the base-layer block;
 * docs/decisions/2026-10-23-scrollbars-are-never-always-visible.md). CSS has no
 * "is scrolling" state, so this sets `data-scrolling` on the element a `scroll`
 * event fires on and clears it once no scroll has arrived for
 * `SCROLL_REVEAL_LINGER_MS`. The reveal rules key on that attribute; the thumb
 * stays grabbable while transparent, because the column it sits in is always
 * there.
 *
 * One capturing listener on the document sees every light-DOM scroll: `scroll`
 * does not bubble, but capture reaches the target from the root. It does NOT
 * see a scroll inside a shadow root, because the event is not composed and so
 * never leaves that root. The two libraries that draw into one here (Pierre's
 * `diffs-container`, the tree's `file-tree-container`) get their own listener
 * on the shadow root: a document-wide MutationObserver watches for those hosts
 * (an element's own tag, or one inside an added subtree) and attaches once per
 * root. Their sheets key on the same attribute through `unsafeCSS`
 * (`PIERRE_SCROLL_REVEAL_CSS`, `PIERRE_TREE_STATE_ROW_CSS` in pierre/config.ts).
 *
 * Idempotent per root; `installScrollReveal` returns the uninstaller the tests
 * use. Passive listeners: nothing here cancels a scroll.
 */

export const SCROLLING_ATTR = 'data-scrolling'
/** How long the thumb stays after the last scroll event. macOS lingers about
 *  this long before fading its overlay bar, long enough to grab the thumb. */
export const SCROLL_REVEAL_LINGER_MS = 800
/** Custom elements that draw into a shadow root and scroll inside it. */
export const SHADOW_SCROLL_HOST_TAGS = ['diffs-container', 'file-tree-container'] as const
/** The same, as the selector `matches` / `querySelectorAll` take. */
export const SHADOW_SCROLL_HOSTS = SHADOW_SCROLL_HOST_TAGS.join(',')

const timers = new WeakMap<Element, ReturnType<typeof setTimeout>>()
const watched = new WeakSet<Document | ShadowRoot>()

function mark(el: Element) {
  if (!el.hasAttribute(SCROLLING_ATTR)) el.setAttribute(SCROLLING_ATTR, '')
  const prior = timers.get(el)
  if (prior !== undefined) clearTimeout(prior)
  timers.set(el, setTimeout(() => {
    timers.delete(el)
    el.removeAttribute(SCROLLING_ATTR)
  }, SCROLL_REVEAL_LINGER_MS))
}

function onScroll(e: Event) {
  // `composedPath()[0]` is the real scroller even when the listener sits on a
  // shadow root; `target` alone is retargeted to the host from outside.
  const el = e.composedPath()[0]
  if (el instanceof Element) mark(el)
}

const LISTEN: AddEventListenerOptions = { capture: true, passive: true }

function watch(root: Document | ShadowRoot) {
  if (watched.has(root)) return
  watched.add(root)
  root.addEventListener('scroll', onScroll, LISTEN)
}

function unwatch(root: Document | ShadowRoot) {
  if (!watched.has(root)) return
  watched.delete(root)
  root.removeEventListener('scroll', onScroll, LISTEN)
}

/** Attach to every shadow-scrolling host at or under `node`. */
function watchHostsIn(node: Node) {
  if (!(node instanceof Element)) return
  if (node.matches(SHADOW_SCROLL_HOSTS) && node.shadowRoot) watch(node.shadowRoot)
  for (const host of node.querySelectorAll(SHADOW_SCROLL_HOSTS)) {
    if (host.shadowRoot) watch(host.shadowRoot)
  }
}

/** Start marking scrollers under `doc`. Returns the uninstaller. */
export function installScrollReveal(doc: Document = document): () => void {
  watch(doc)
  watchHostsIn(doc.documentElement)
  // Proportionate to what streaming adds: text nodes are skipped outright, and
  // an element without children (a span, a `<br>`, a leaf row) needs only the
  // one `matches()`; the subtree query runs for an added subtree alone.
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const added of record.addedNodes) {
        if (!(added instanceof Element)) continue
        if (added.firstElementChild === null) {
          if (added.matches(SHADOW_SCROLL_HOSTS) && added.shadowRoot) watch(added.shadowRoot)
          continue
        }
        watchHostsIn(added)
      }
    }
  })
  observer.observe(doc.documentElement, { childList: true, subtree: true })
  return () => {
    observer.disconnect()
    unwatch(doc)
    for (const host of doc.querySelectorAll(SHADOW_SCROLL_HOSTS)) {
      if (host.shadowRoot) unwatch(host.shadowRoot)
    }
  }
}
