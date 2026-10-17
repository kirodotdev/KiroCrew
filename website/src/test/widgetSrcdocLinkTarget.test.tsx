/** Executes the REAL external-link target shim -- extracted from buildSrcdoc's
 * own output, not a copy -- against a live document, and pins the contract:
 *
 *   A bare absolute `http(s):` link inside a widget/artifact opens OUTSIDE the
 *   sandboxed frame (target=_blank + rel=noopener noreferrer), so it
 *   reaches the host's window-open handler -- the user's default browser in the
 *   desktop app -- instead of replacing the widget body with a cookie-less copy
 *   of the target site. Fragment links, relative paths, `mailto:` and other
 *   non-web schemes, anchors with an explicit target, and data-action composer
 *   buttons are left exactly as written. Hosts whose sandbox withholds
 *   `allow-popups` opt out with `rewriteBareLinks: false` and get no shim.
 *
 * The rewrite happens at click time, on the capture phase of `window`, so it
 * precedes any widget-authored click handler -- even a capture listener on
 * `window` that stops propagation; the assertion below reads the attributes the
 * anchor carries once dispatch completes, which is what the browser's activation
 * behaviour reads.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'

function extractLinkShim(): string {
  const out = buildSrcdoc({ html: '<p>probe</p>', themeVars: {}, mode: 'dark' })
  const doc = new DOMParser().parseFromString(out, 'text/html')
  const script = Array.from(doc.querySelectorAll('script'))
    .map((s) => s.textContent ?? '')
    .find((t) => t.includes('new URL(') && t.includes("'_blank'"))
  if (!script) throw new Error('external-link target shim not found in srcdoc')
  return script
}

/** Mount `bodyHtml` in the real test document, install the shim against it,
 * click the anchor, and return the anchor for inspection. The click's default
 * navigation is cancelled by a bubbling listener so the test runner's document
 * is not navigated; the shim runs on capture, before that listener. */
function clickInShimmedDocument(bodyHtml: string, selector: string, clickSelector = selector): HTMLAnchorElement {
  const host = document.createElement('div')
  host.setAttribute('data-shim-host', '')
  // DOMParser, not innerHTML: the fixture markup is parsed in a detached
  // document and its nodes adopted, the same typed-DOM route buildSrcdoc uses.
  const parsed = new DOMParser().parseFromString(bodyHtml, 'text/html')
  Array.from(parsed.body.childNodes).forEach((n) => host.appendChild(document.importNode(n, true)))
  document.body.appendChild(host)
  new Function('document', extractLinkShim())(document)
  const a = host.querySelector<HTMLAnchorElement>(selector)
  if (!a) throw new Error(`no anchor for ${selector}`)
  const clickTarget = host.querySelector<HTMLElement>(clickSelector)
  if (!clickTarget) throw new Error(`no click target for ${clickSelector}`)
  a.addEventListener('click', (e) => e.preventDefault())
  clickTarget.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
  return a
}

describe('external-link target shim', () => {
  afterEach(() => {
    document.querySelectorAll('[data-shim-host]').forEach((n) => n.remove())
  })

  it('is injected on the default surface, before the LLM body', () => {
    const out = buildSrcdoc({ html: '<p id="llm">body</p>', themeVars: {}, mode: 'dark' })
    const doc = new DOMParser().parseFromString(out, 'text/html')
    const nodes = Array.from(doc.body.children)
    const shimIdx = nodes.findIndex((n) => n.tagName === 'SCRIPT' && (n.textContent ?? '').includes("'_blank'"))
    const bodyIdx = nodes.findIndex((n) => n.id === 'llm')
    expect(shimIdx).toBeGreaterThanOrEqual(0)
    expect(bodyIdx).toBeGreaterThan(shimIdx)
  })

  it('rewrites a bare https link to _blank + noopener noreferrer', () => {
    const a = clickInShimmedDocument(
      '<a id="cr" href="https://reviews.example.test/reviews/CR-1">CR-1</a>',
      '#cr',
    )
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('rewrites an href the browser would navigate after whitespace stripping', () => {
    // Leading/trailing ASCII whitespace and embedded tab/newline are removed by
    // the URL parser, so the browser navigates these; a raw-attribute regex
    // would have missed them and left the link in-frame.
    const a = clickInShimmedDocument(
      '<a id="ws" href="  https://exam\nple.test/path ">spaced</a>',
      '#ws',
    )
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('runs ahead of a widget capture listener on window that stops propagation', () => {
    const host = document.createElement('div')
    host.setAttribute('data-shim-host', '')
    const a = document.createElement('a')
    a.id = 'late'
    a.href = 'https://example.test/late'
    a.textContent = 'late'
    host.appendChild(a)
    document.body.appendChild(host)
    new Function('document', extractLinkShim())(document)
    // Widget-authored: registered AFTER the shim (the shim script precedes the
    // LLM body), on the outermost capture target.
    const stopper = (e: Event) => e.stopPropagation()
    window.addEventListener('click', stopper, true)
    try {
      a.addEventListener('click', (e) => e.preventDefault())
      a.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    } finally {
      window.removeEventListener('click', stopper, true)
    }
    expect(a.getAttribute('target')).toBe('_blank')
  })

  it('finds an anchor inside an open shadow root (composedPath, not the retargeted host)', () => {
    const host = document.createElement('div')
    host.setAttribute('data-shim-host', '')
    const component = document.createElement('x-card')
    const root = component.attachShadow({ mode: 'open' })
    const a = document.createElement('a')
    a.href = 'https://example.test/shadow'
    a.textContent = 'shadow'
    root.appendChild(a)
    host.appendChild(component)
    document.body.appendChild(host)
    new Function('document', extractLinkShim())(document)
    a.addEventListener('click', (e) => e.preventDefault())
    a.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, composed: true }))
    expect(a.getAttribute('target')).toBe('_blank')
  })

  it('is omitted when the host opts out (rewriteBareLinks: false)', () => {
    const out = buildSrcdoc({ html: '<p>probe</p>', themeVars: {}, mode: 'dark', rewriteBareLinks: false })
    const doc = new DOMParser().parseFromString(out, 'text/html')
    const shims = Array.from(doc.querySelectorAll('script'))
      .filter((n) => (n.textContent ?? '').includes("'_blank'"))
    expect(shims).toHaveLength(0)
  })

  it('rewrites when the click lands on a child of the anchor', () => {
    const a = clickInShimmedDocument(
      '<a id="wrap" href="http://example.test/x"><span id="inner">inner</span></a>',
      '#wrap',
      '#inner',
    )
    expect(a.getAttribute('target')).toBe('_blank')
  })

  it.each([
    ['fragment', '<a id="t" href="#section-2">jump</a>'],
    ['relative path', '<a id="t" href="/artifacts/slug">open</a>'],
    ['relative file', '<a id="t" href="other.html">open</a>'],
    ['javascript pseudo-scheme', '<a id="t" href="javascript:void(0)">noop</a>'],
    // The desktop host's window-open handler denies non-web schemes, so a
    // `_blank` mailto would be a dead click there; it stays a plain navigation.
    ['mailto', '<a id="t" href="mailto:someone@example.test">mail</a>'],
  ])('leaves a %s link without a target', (_label, html) => {
    const a = clickInShimmedDocument(html, '#t')
    expect(a.hasAttribute('target')).toBe(false)
    expect(a.hasAttribute('rel')).toBe(false)
  })

  it('respects an explicit target (author intent), including _self', () => {
    const a = clickInShimmedDocument(
      '<a id="s" href="https://example.test/" target="_self">stay</a>',
      '#s',
    )
    expect(a.getAttribute('target')).toBe('_self')
    expect(a.hasAttribute('rel')).toBe(false)
  })

  it('skips data-action anchors (composer pre-fill buttons)', () => {
    const a = clickInShimmedDocument(
      '<a id="act" href="https://example.test/ignored" data-action="approve">Approve</a>',
      '#act',
    )
    expect(a.hasAttribute('target')).toBe(false)
  })
})
