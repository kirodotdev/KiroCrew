/** Executes the REAL link-bridge shim -- extracted from buildSrcdoc's own output,
 * not a copy -- and pins what a frame holding an agent-written page does with a
 * link once the frame grants no popups:
 *
 *   A click on a `_blank` (or any non-`_self` target) link, or on a bare absolute
 *   `http(s)` link, is cancelled in the frame and handed to the host as
 *   `{type: 'kirocrew-dashboard:open', url}`. `window.open` posts the same and
 *   returns null. A non-web scheme on a window-opening link is cancelled and NOT
 *   posted. Fragment and relative links with no target, an explicit `_self`, and
 *   data-action buttons keep their in-frame behaviour. A document with no parent
 *   (a downloaded copy) gets no bridge at all.
 *
 * The host side (gesture, URL check, confirm, open) is pinned in
 * hooks/useFrameLinkBridge.test.tsx.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'

const OPEN = 'kirocrew-dashboard:open'

function extractBridge(): string {
  const out = buildSrcdoc({ html: '<p>probe</p>', themeVars: {}, mode: 'dark', linkBridge: true })
  const doc = new DOMParser().parseFromString(out, 'text/html')
  const script = Array.from(doc.querySelectorAll('script'))
    .map((s) => s.textContent ?? '')
    .find((t) => t.includes(OPEN))
  if (!script) throw new Error('link-bridge shim not found in srcdoc')
  return script
}

interface Installed {
  posted: unknown[]
  win: { open?: (u: unknown) => unknown; parent: unknown }
}

const removers: Array<() => void> = []

/** Run the shim as a framed document would: `window` is a stand-in whose
 * listeners land on the real test window, and `parent` is a separate object
 * that records what was posted. `topLevel` makes `window.parent === window`. */
function install(topLevel = false): Installed {
  const posted: unknown[] = []
  const parentStub = { postMessage: (data: unknown) => { posted.push(data) } }
  const win: Installed['win'] & { addEventListener: typeof window.addEventListener } = {
    parent: parentStub,
    addEventListener: ((type: string, fn: EventListener, opts?: boolean | AddEventListenerOptions) => {
      window.addEventListener(type, fn, opts)
      removers.push(() => window.removeEventListener(type, fn, opts))
    }) as typeof window.addEventListener,
  }
  if (topLevel) win.parent = win
  new Function('window', 'parent', extractBridge())(win, parentStub)
  return { posted, win }
}

function mount(html: string): HTMLElement {
  const host = document.createElement('div')
  host.setAttribute('data-bridge-host', '')
  const parsed = new DOMParser().parseFromString(html, 'text/html')
  Array.from(parsed.body.childNodes).forEach((n) => host.appendChild(document.importNode(n, true)))
  document.body.appendChild(host)
  return host
}

/** Click `selector` and report whether the click's default was cancelled. A
 * bubbling listener cancels it afterwards either way, so the test document is
 * never navigated; it records the state the shim left first. */
function click(host: HTMLElement, selector: string): boolean {
  const el = host.querySelector<HTMLElement>(selector)
  if (!el) throw new Error(`nothing matches ${selector}`)
  let prevented = false
  const after = (e: Event) => { prevented = e.defaultPrevented; e.preventDefault() }
  window.addEventListener('click', after)
  try {
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
  } finally {
    window.removeEventListener('click', after)
  }
  return prevented
}

afterEach(() => {
  removers.splice(0).forEach((r) => r())
  document.querySelectorAll('[data-bridge-host]').forEach((n) => n.remove())
})

describe('link-bridge shim', () => {
  it('replaces the _blank rewrite and runs before the LLM body', () => {
    const out = buildSrcdoc({ html: '<p id="llm">body</p>', themeVars: {}, mode: 'dark', linkBridge: true })
    const doc = new DOMParser().parseFromString(out, 'text/html')
    const nodes = Array.from(doc.body.children)
    const shimIdx = nodes.findIndex((n) => n.tagName === 'SCRIPT' && (n.textContent ?? '').includes(OPEN))
    expect(shimIdx).toBeGreaterThanOrEqual(0)
    expect(nodes.findIndex((n) => n.id === 'llm')).toBeGreaterThan(shimIdx)
    // The old rewrite would turn a bare link into a popup the frame cannot open.
    const rewrite = nodes.filter((n) => n.tagName === 'SCRIPT' && (n.textContent ?? '').includes("setAttribute('target', '_blank')"))
    expect(rewrite).toHaveLength(0)
  })

  it('is not injected unless asked for, so an exported copy keeps ordinary links', () => {
    const out = buildSrcdoc({ html: '<p>probe</p>', themeVars: {}, mode: 'dark' })
    expect(out).not.toContain(OPEN)
  })

  it('leaves the CDN script origins in the policy exactly as before', () => {
    const plain = buildSrcdoc({ html: '<p>x</p>', themeVars: {}, mode: 'dark' })
    const bridged = buildSrcdoc({ html: '<p>x</p>', themeVars: {}, mode: 'dark', linkBridge: true })
    const csp = (html: string) =>
      new DOMParser().parseFromString(html, 'text/html')
        .querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content')
    expect(csp(bridged)).toBe(csp(plain))
    expect(csp(bridged)).toContain('https://cdn.jsdelivr.net https://cdnjs.cloudflare.com')
  })

  it('hands a _blank web link to the host and cancels it in the frame', () => {
    const { posted } = install()
    const host = mount('<a id="gh" href="https://github.com/example-org/repo/pull/7" target="_blank" rel="noopener noreferrer">PR</a>')
    expect(click(host, '#gh')).toBe(true)
    expect(posted).toEqual([{ type: OPEN, url: 'https://github.com/example-org/repo/pull/7' }])
  })

  it('hands a bare absolute web link to the host instead of navigating the frame', () => {
    const { posted } = install()
    const host = mount('<a id="cr" href="https://code.example.test/reviews/CR-1"><span id="in">CR-1</span></a>')
    expect(click(host, '#in')).toBe(true)
    expect(posted).toEqual([{ type: OPEN, url: 'https://code.example.test/reviews/CR-1' }])
  })

  it('treats any target other than _self as a window open', () => {
    const { posted } = install()
    const host = mount('<a id="n" href="http://example.test/x" target="report">x</a>')
    expect(click(host, '#n')).toBe(true)
    expect(posted).toEqual([{ type: OPEN, url: 'http://example.test/x' }])
  })

  it.each([
    ['javascript', 'javascript:alert(1)'],
    ['data', 'data:text/html,<p>x</p>'],
    ['mailto', 'mailto:someone@example.test'],
    ['file', 'file:///etc/hosts'],
    ['relative', '/artifacts/slug'],
  ])('cancels a _blank %s link and posts nothing', (_label, href) => {
    const { posted } = install()
    const host = mount(`<a id="t" target="_blank">x</a>`)
    host.querySelector('#t')!.setAttribute('href', href)
    expect(click(host, '#t')).toBe(true)
    expect(posted).toEqual([])
  })

  it.each([
    ['fragment', '<a id="t" href="#part-2">jump</a>'],
    ['relative path', '<a id="t" href="/artifacts/slug">open</a>'],
    ['mailto', '<a id="t" href="mailto:someone@example.test">mail</a>'],
    ['explicit _self', '<a id="t" href="https://example.test/" target="_self">stay</a>'],
    ['data-action button', '<a id="t" href="https://example.test/x" data-action="approve">Approve</a>'],
  ])('leaves a %s link to the frame', (_label, html) => {
    const { posted } = install()
    const host = mount(html)
    expect(click(host, '#t')).toBe(false)
    expect(posted).toEqual([])
  })

  it('turns window.open into a host request for a web URL only', () => {
    const { posted, win } = install()
    expect(win.open?.('https://example.test/a')).toBeNull()
    win.open?.('javascript:alert(1)')
    win.open?.('data:text/html,x')
    win.open?.('/relative')
    expect(posted).toEqual([{ type: OPEN, url: 'https://example.test/a' }])
  })

  it('does nothing in a document with no parent', () => {
    const { posted, win } = install(true)
    const host = mount('<a id="t" href="https://example.test/" target="_blank">x</a>')
    expect(click(host, '#t')).toBe(false)
    expect(win.open).toBeUndefined()
    expect(posted).toEqual([])
  })
})
