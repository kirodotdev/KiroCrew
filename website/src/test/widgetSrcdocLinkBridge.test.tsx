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
 * The host side (gesture, URL check, dashboard-input wait, open) is pinned in
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

/** Click `selector` and report whether the click's default was cancelled by the
 * end of its dispatch. (The test document does not navigate on a link click.) */
function click(host: HTMLElement, selector: string, init: MouseEventInit = {}): boolean {
  const el = host.querySelector<HTMLElement>(selector)
  if (!el) throw new Error(`nothing matches ${selector}`)
  // dispatchEvent answers false when the click was cancelled by the time its default
  // would run, after every listener (the shim's last one included).
  return !el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, ...init }))
}

/** The shim posts a task after the click, once every page handler has run. */
const settle = () => new Promise<void>((resolve) => { setTimeout(resolve, 0) })

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
    // The old rewrite alone would turn a bare link into a popup the frame cannot open.
    const rewrite = nodes.filter((n) => {
      const text = n.tagName === 'SCRIPT' ? n.textContent ?? '' : ''
      return text.includes("setAttribute('target', '_blank')") && !text.includes(OPEN)
    })
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

  it('hands a _blank web link to the host after the click, leaving its window to the sandbox', async () => {
    const { posted } = install()
    const host = mount('<a id="gh" href="https://github.com/example-org/repo/pull/7" target="_blank" rel="noopener noreferrer">PR</a>')
    // Not cancelled: the frame's sandbox refuses the window a plain click would open.
    expect(click(host, '#gh')).toBe(false)
    expect(posted).toEqual([])
    await settle()
    expect(posted).toEqual([{ type: OPEN, url: 'https://github.com/example-org/repo/pull/7' }])
  })

  it('hands a bare absolute web link to the host instead of navigating the frame', async () => {
    const { posted } = install()
    const host = mount('<a id="cr" href="https://code.example.test/reviews/CR-1"><span id="in">CR-1</span></a>')
    click(host, '#in')
    // Window-opening now, so the sandbox refuses it instead of the frame navigating.
    expect(host.querySelector('#cr')!.getAttribute('target')).toBe('_blank')
    await settle()
    expect(posted).toEqual([{ type: OPEN, url: 'https://code.example.test/reviews/CR-1' }])
  })

  it.each([['ctrlKey'], ['metaKey'], ['shiftKey']])('cancels a %s click at once, since it opens a tab even in the sandbox', async (key) => {
    const { posted } = install()
    const host = mount('<a id="t" href="https://example.test/m">x</a>')
    expect(click(host, '#t', { [key]: true })).toBe(true)
    await settle()
    expect(posted).toEqual([{ type: OPEN, url: 'https://example.test/m' }])
  })

  it.each([
    ['in a window listener added after the shim', () => {
      const fn = (e: Event) => e.preventDefault()
      window.addEventListener('click', fn)
      removers.push(() => window.removeEventListener('click', fn))
    }],
  ])('posts nothing for a modified click the page cancels %s', async (_label, cancel) => {
    const { posted } = install()
    const host = mount('<a id="t" href="https://example.test/m">x</a>')
    cancel()
    expect(click(host, '#t', { ctrlKey: true })).toBe(true)
    await settle()
    expect(posted).toEqual([])
  })

  it('posts nothing for a modified click the page cancels', async () => {
    const { posted } = install()
    const host = mount('<a id="t" href="https://example.test/m" target="_blank">x</a>')
    host.querySelector('#t')!.addEventListener('click', (e) => e.preventDefault())
    click(host, '#t', { ctrlKey: true })
    await settle()
    expect(posted).toEqual([])
  })

  it.each([
    ['cancels it in a handler', (a: HTMLElement) => a.addEventListener('click', (e) => e.preventDefault())],
    ['cancels it on document', () => {
      const fn = (e: Event) => e.preventDefault()
      document.addEventListener('click', fn)
      removers.push(() => document.removeEventListener('click', fn))
    }],
    ['cancels it on window, registered after the shim', () => {
      const fn = (e: Event) => e.preventDefault()
      window.addEventListener('click', fn)
      removers.push(() => window.removeEventListener('click', fn))
    }],
  ])('posts nothing when the page %s', async (_label, cancel) => {
    const { posted } = install()
    const host = mount('<a id="p" href="https://example.test/panel" target="_blank">panel</a><a id="b" href="https://example.test/bare">bare</a>')
    cancel(host.querySelector<HTMLElement>('#p')!)
    cancel(host.querySelector<HTMLElement>('#b')!)
    expect(click(host, '#p')).toBe(true)
    expect(click(host, '#b')).toBe(true)
    await settle()
    expect(posted).toEqual([])
  })

  it('keeps a bare link from navigating the frame when the page stops the click', async () => {
    const { posted } = install()
    const host = mount('<a id="b" href="https://example.test/bare"><span id="in">bare</span></a>')
    host.querySelector('#in')!.addEventListener('click', (e) => e.stopPropagation())
    let target: string | null = null
    host.querySelector('#in')!.addEventListener('click', (e) => {
      target = (e.currentTarget as HTMLElement).closest('a')!.getAttribute('target')
      e.preventDefault()
    })
    host.querySelector('#in')!.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    // A window-opening link, which the sandbox refuses, instead of a navigation.
    expect(target).toBe('_blank')
    await settle()
    expect(posted).toEqual([])
  })

  it('posts nothing for a middle-click the page cancels', async () => {
    const { posted } = install()
    const host = mount('<a id="t" href="https://example.test/m" target="_blank">x</a>')
    host.querySelector('#t')!.addEventListener('auxclick', (e) => e.preventDefault())
    middle(host, '#t')
    await settle()
    expect(posted).toEqual([])
  })

  it.each([
    ['a _blank', '<a id="t" href="//example.test/docs" target="_blank">x</a>'],
    ['a bare', '<a id="t" href="//example.test/docs">x</a>'],
  ])('hands %s protocol-relative link to the host with the base scheme', async (_label, html) => {
    const { posted } = install()
    const host = mount(html)
    click(host, '#t')
    await settle()
    const scheme = new URL(document.baseURI).protocol === 'http:' ? 'http:' : 'https:'
    expect(posted).toEqual([{ type: OPEN, url: `${scheme}//example.test/docs` }])
  })

  it('hands an SVG link that carries xlink:href to the host, and titles it on hover', async () => {
    const { posted } = install()
    const host = mount('<svg><a id="s" target="_blank"><text id="label">docs</text></a></svg>')
    const a = host.querySelector('#s')!
    a.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', 'https://example.test/svg')
    host.querySelector('#label')!.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))
    expect(a.getAttribute('title')).toBe('https://example.test/svg')
    host.querySelector('#label')!.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    await settle()
    expect(posted).toEqual([{ type: OPEN, url: 'https://example.test/svg' }])
  })

  it('treats any target other than _self as a window open', async () => {
    const { posted } = install()
    const host = mount('<a id="n" href="http://example.test/x" target="report">x</a>')
    click(host, '#n')
    await settle()
    expect(posted).toEqual([{ type: OPEN, url: 'http://example.test/x' }])
  })

  it.each([
    ['javascript', 'javascript:alert(1)'],
    ['data', 'data:text/html,<p>x</p>'],
    ['mailto', 'mailto:someone@example.test'],
    ['file', 'file:///etc/hosts'],
    ['relative', '/artifacts/slug'],
  ])('leaves a _blank %s link to the sandbox and posts nothing', async (_label, href) => {
    const { posted } = install()
    const host = mount(`<a id="t" target="_blank">x</a>`)
    host.querySelector('#t')!.setAttribute('href', href)
    click(host, '#t')
    await settle()
    expect(posted).toEqual([])
  })

  it.each([
    ['fragment', '<a id="t" href="#part-2">jump</a>'],
    ['relative path', '<a id="t" href="/artifacts/slug">open</a>'],
    ['mailto', '<a id="t" href="mailto:someone@example.test">mail</a>'],
    ['explicit _self', '<a id="t" href="https://example.test/" target="_self">stay</a>'],
    ['data-action button', '<a id="t" href="https://example.test/x" data-action="approve">Approve</a>'],
  ])('leaves a %s link to the frame', async (_label, html) => {
    const { posted } = install()
    const host = mount(html)
    expect(click(host, '#t')).toBe(false)
    await settle()
    expect(posted).toEqual([])
  })

  function middle(host: HTMLElement, selector: string, button = 1): boolean {
    const el = host.querySelector<HTMLElement>(selector)!
    return !el.dispatchEvent(new MouseEvent('auxclick', { bubbles: true, cancelable: true, button }))
  }

  it.each([
    ['_blank', '<a id="t" href="https://example.test/m" target="_blank">x</a>'],
    ['bare', '<a id="t" href="https://example.test/m">x</a>'],
    ['_self', '<a id="t" href="https://example.test/m" target="_self">x</a>'],
  ])('hands a middle-clicked %s web link to the host', async (_label, html) => {
    const { posted } = install()
    const host = mount(html)
    // Cancelled at once: a middle-click opens a tab even in the sandbox.
    expect(middle(host, '#t')).toBe(true)
    await settle()
    expect(posted).toEqual([{ type: OPEN, url: 'https://example.test/m' }])
  })

  it('leaves a middle-clicked fragment link and a right-click alone', async () => {
    const { posted } = install()
    const host = mount('<a id="f" href="#part">f</a><a id="w" href="https://example.test/r">w</a>')
    expect(middle(host, '#f')).toBe(false)
    expect(middle(host, '#w', 2)).toBe(false)
    await settle()
    expect(posted).toEqual([])
  })

  it('finds a link that wraps a custom element whose shadow content was clicked', async () => {
    const { posted } = install()
    const host = mount('<a id="wrap" href="https://example.test/shadow" target="_blank"><x-label id="lbl"></x-label></a>')
    const label = host.querySelector('#lbl')!
    const inner = document.createElement('span')
    inner.textContent = 'open'
    label.attachShadow({ mode: 'open' }).appendChild(inner)
    let prevented = false
    const after = (e: Event) => { prevented = e.defaultPrevented }
    window.addEventListener('click', after)
    try {
      inner.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, composed: true }))
    } finally {
      window.removeEventListener('click', after)
    }
    expect(prevented).toBe(false)
    await settle()
    expect(posted).toEqual([{ type: OPEN, url: 'https://example.test/shadow' }])
  })

  it('shows a web link URL on hover or focus as its title, keeping an author title', () => {
    install()
    const host = mount(
      '<a id="w" href="HTTPS://Example.TEST/a b">w</a>' +
      '<a id="t" href="https://example.test/x" title="Own title">t</a>' +
      '<a id="f" href="#part">f</a>' +
      '<a id="j" href="javascript:void(0)">j</a>',
    )
    const over = (sel: string) =>
      host.querySelector(sel)!.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))
    over('#w')
    over('#t')
    over('#f')
    host.querySelector<HTMLElement>('#j')!.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    expect(host.querySelector('#w')!.getAttribute('title')).toBe('https://example.test/a%20b')
    expect(host.querySelector('#t')!.getAttribute('title')).toBe('Own title')
    expect(host.querySelector('#f')!.hasAttribute('title')).toBe(false)
    expect(host.querySelector('#j')!.hasAttribute('title')).toBe(false)
  })

  it('reports each click or key in the frame, so the host can hold a sibling\'s open', () => {
    const { posted } = install()
    const host = mount('<button id="b">b</button>')
    const b = host.querySelector('#b')!
    b.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }))
    b.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    b.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))
    expect(posted).toEqual([{ type: 'kirocrew-dashboard:input' }, { type: 'kirocrew-dashboard:input' }])
  })

  it('turns window.open into a host request for a web URL only', () => {
    const { posted, win } = install()
    expect(win.open?.('https://example.test/a')).toBeNull()
    win.open?.('javascript:alert(1)')
    win.open?.('data:text/html,x')
    win.open?.('/relative')
    expect(posted).toEqual([{ type: OPEN, url: 'https://example.test/a' }])
  })

  it('leaves window.open writable and configurable, so a page declaring its own open still runs', () => {
    const { win } = install()
    const desc = Object.getOwnPropertyDescriptor(win, 'open')
    expect(desc?.writable).toBe(true)
    expect(desc?.configurable).toBe(true)
    const mine = () => 'page-owned'
    win.open = mine
    expect(win.open).toBe(mine)
  })

  it('does nothing in a document with no parent', async () => {
    const { posted, win } = install(true)
    const host = mount('<a id="t" href="https://example.test/" target="_blank">x</a>')
    expect(click(host, '#t')).toBe(false)
    expect(win.open).toBeUndefined()
    await settle()
    expect(posted).toEqual([])
  })
})
