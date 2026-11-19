/**
 * The walker's `read` method (`src/kiro_crew/browser_harness/snapshot_walker.js`),
 * which serves the `browser` tool's `read` op: the page's content as text.
 *
 * Text is the root's rendered text for the whole page, not only the viewport,
 * plus the open shadow roots and same-origin frames `innerText` skips; a frame
 * this world cannot read is counted, never read, and a root with no box has no
 * text. The live DOM is unchanged.
 */
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setHtml } from './domFixture'
import { readSource } from './readSource'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readSource(join(HARNESS_DIR, 'snapshot_walker.js')).split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

interface ReadResult {
  ok: boolean
  code?: string
  content?: string
  skippedFrames?: number
  deepRoots?: number
  truncated?: boolean
}
interface Walker {
  read: (ref: string | null) => ReadResult
  snapshot: () => { nodes: Array<{ ref?: string; role: string; name: string }> }
}

const walker = (): Walker => new Function(`return (${walkerSource()})`)() as Walker

const realElementRect = Element.prototype.getBoundingClientRect
const realRangeRect = Range.prototype.getBoundingClientRect
const realClientRects = Element.prototype.getClientRects
const realRangeRects = Range.prototype.getClientRects
const realOffsetParent = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent')
const realComputedStyle = window.getComputedStyle.bind(window)

/** happy-dom omits the initial visibility value; pin it and any layout-only styles. */
function computedStyles(entries: Array<[Element, Partial<CSSStyleDeclaration>]> = []): void {
  const overrides = new Map(entries)
  vi.spyOn(window, 'getComputedStyle').mockImplementation((el) => new Proxy(realComputedStyle(el), {
    get(style, key) {
      const value = overrides.get(el)?.[key as keyof CSSStyleDeclaration] ?? Reflect.get(style, key)
      return key === 'visibility' ? value || 'visible' : value
    },
  }))
}

function box(top: number): DOMRect {
  return { x: 0, y: top, left: 0, top, width: 100, height: 20, right: 100, bottom: top + 20, toJSON: () => ({}) } as DOMRect
}

/** Give every element a box so `visible()` lets the snapshot see it. */
function withBoxes(): void {
  Element.prototype.getBoundingClientRect = () => box(0)
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', { configurable: true, get: () => document.body })
}

/** The composed parent: the assigned slot, the shadow host, or the parent node. */
function up(n: Node): Node | null {
  const slot = (n as Element | Text).assignedSlot
  if (slot) return slot
  const p = n.parentNode
  return p instanceof ShadowRoot ? p.host : p
}

/** A light-DOM child of a shadow host that no slot takes is not rendered. */
function unslotted(n: Node): boolean {
  const p = n.parentNode
  return p instanceof Element && !!p.shadowRoot && !(n as Element | Text).assignedSlot
}

/**
 * A stand-in for layout. Nothing under a `hidden` element draws a box, nor does an
 * unassigned slottable; a `boxless` element (`display: contents`) draws none of its
 * own while its children still do. Text ranges follow the same rule.
 */
function layout(hidden: (el: Element) => boolean, boxless: (el: Element) => boolean = () => false): void {
  const shown = (n: Node): boolean => {
    for (let c: Node | null = n; c; c = up(c)) {
      if (unslotted(c) || (c instanceof Element && hidden(c))) return false
    }
    return true
  }
  Element.prototype.getClientRects = function (this: Element) {
    return (shown(this) && !boxless(this) ? [box(0)] : []) as unknown as DOMRectList
  }
  Range.prototype.getClientRects = function (this: Range) {
    return (shown(this.startContainer) ? [box(0)] : []) as unknown as DOMRectList
  }
}

/** Elements matching `selector` (and everything inside them) have no box, as with `display:none`. */
function unrendered(selector: string): void {
  layout((el) => el.matches(selector))
}

const contents = (el: Element): boolean => el.localName === 'x-article'

function sameOriginFrame(frame: Element, html: string): Document {
  const doc = document.implementation.createHTMLDocument('inner')
  setHtml(doc.body, html)
  Object.defineProperty(frame, 'contentDocument', { get: () => doc, configurable: true })
  Object.defineProperty(doc, 'defaultView', { configurable: true, value: {
    frameElement: frame,
    getComputedStyle: (el: Element) => window.getComputedStyle(el),
  } })
  return doc
}

beforeEach(() => {
  layout(() => false)
  computedStyles()
})

afterEach(() => {
  vi.restoreAllMocks()
  setHtml(document.body, '')
  Element.prototype.getBoundingClientRect = realElementRect
  Range.prototype.getBoundingClientRect = realRangeRect
  Element.prototype.getClientRects = realClientRects
  Range.prototype.getClientRects = realRangeRects
  if (realOffsetParent) Object.defineProperty(HTMLElement.prototype, 'offsetParent', realOffsetParent)
})

describe('read, text', () => {
  it('returns text far below the first screen, not only what is in view', () => {
    const filler = Array.from({ length: 200 }, (_, i) => `<p>line ${i}</p>`).join('')
    setHtml(document.body, `<h1>Top</h1>${filler}<p>the very last line</p>`)
    // A viewport reader measures each text range: all but the heading's is off screen.
    Range.prototype.getBoundingClientRect = function (this: Range) {
      const el = this.startContainer.parentElement
      return box(el && el.tagName === 'H1' ? 0 : 5000)
    }
    const out = walker().read(null)
    expect(out.ok).toBe(true)
    expect(out.content).toContain('Top')
    expect(out.content).toContain('line 199')
    expect(out.content).toContain('the very last line')
  })

  it('leaves out a secret-named textarea in a shadow root', () => {
    setHtml(document.body, '<p>Sign in</p><div id="host"></div>')
    const root = document.getElementById('host')!.attachShadow({ mode: 'open' })
    setHtml(root, '<p>shadow words</p><textarea name="apiToken">shadow-shh</textarea>')
    const out = walker().read(null)
    expect(out.content).toContain('Sign in')
    expect(out.content).toContain('shadow words')
    expect(out.content).not.toContain('shadow-shh')
  })

  it.each(['scoped', 'shadow'])('omits a prefilled textarea from a %s SVG read', (scope) => {
    withBoxes()
    setHtml(document.body, '<p>outside the SVG</p><div id="host"></div>')
    const host = document.getElementById('host')!
    const root = scope === 'shadow' ? host.attachShadow({ mode: 'open' }) : host
    setHtml(root, '<svg role="button" aria-label="diagram"><text>VISIBLE-SVG-TEXT</text>' +
      '<foreignObject><textarea name="otp">123456</textarea><p>FO-PROSE</p></foreignObject></svg>')
    // Chromium SVG elements have no innerText; keep happy-dom on that path too.
    for (const el of root.querySelectorAll('*')) {
      if (el.namespaceURI === 'http://www.w3.org/2000/svg') {
        Object.defineProperty(el, 'innerText', { configurable: true, value: undefined })
      }
    }
    const api = walker()
    const ref = api.snapshot().nodes.find((n) => n.name === 'diagram')!.ref!
    expect(ref).toBeTruthy()
    const out = api.read(scope === 'scoped' ? ref : null)
    expect(out.ok).toBe(true)
    expect(out.content).toContain('VISIBLE-SVG-TEXT')
    expect(out.content).toContain('FO-PROSE')
    expect(out.content).not.toContain('123456')
    if (scope === 'scoped') expect(out.content).not.toContain('outside the SVG')
  })

  it('includes the text of an open shadow root, its direct text nodes, and a same-origin frame', () => {
    setHtml(document.body, '<p>light</p><div id="host"></div><iframe id="f"></iframe>')
    const root = document.getElementById('host')!.attachShadow({ mode: 'open' })
    setHtml(root, '<span>inside the shadow</span><style>.x{}</style>')
    root.appendChild(document.createTextNode('a direct shadow text node'))
    sameOriginFrame(document.getElementById('f')!, '<p>inside the frame</p>')
    const out = walker().read(null)
    expect(out.content).toContain('light')
    expect(out.content).toContain('inside the shadow')
    expect(out.content).toContain('a direct shadow text node')
    expect(out.content).toContain('inside the frame')
    expect(out.content).not.toContain('.x{}')
    expect(out.skippedFrames).toBe(0)
  })

  it('gives a root with no box no text, so a hidden script body never leaks', () => {
    setHtml(document.body, '<p>shown</p><div id="hid" class="gone" role="button" aria-label="hid"><script>var k = "SCRIPT-BODY"</script>hidden words</div>')
    withBoxes()
    const api = walker()
    // The snapshot runs while the element still has a box, to get a ref on it.
    const ref = api.snapshot().nodes.find((n) => n.name === 'hid')!.ref!
    unrendered('.gone')
    expect(api.read(null).content).toContain('shown')
    // A ref on the hidden element itself: its innerText would be its textContent.
    const out = api.read(ref)
    expect(out.ok).toBe(true)
    expect(out.content).toBe('')
  })

  it('reads nothing from a hidden same-origin frame or a hidden shadow host', () => {
    setHtml(document.body, '<p>page</p><iframe id="f"></iframe><div id="host" class="gone"></div>')
    const doc = sameOriginFrame(document.getElementById('f')!, '<p>FRAME-TEXT</p><script>var s = "FRAME-SCRIPT"</script>')
    setHtml(document.getElementById('host')!.attachShadow({ mode: 'open' }), 'SHADOW-TEXT')
    layout((el) => el === doc.body || el.classList.contains('gone'))
    const out = walker().read(null)
    expect(out.content).toContain('page')
    expect(out.content).not.toContain('FRAME-TEXT')
    expect(out.content).not.toContain('FRAME-SCRIPT')
    expect(out.content).not.toContain('SHADOW-TEXT')
  })

  it.each([
    ['hidden frame', '', 'visibility:hidden', false],
    ['display:none parent', 'display:none', '', false],
    ['visible frame', '', '', true],
    ['restored frame visibility', 'visibility:hidden', 'visibility:visible', true],
    ['content-visibility:hidden parent', 'content-visibility:hidden', '', false],
  ])('checks frame visibility for %s', (_name, parentStyle, frameStyle, shown) => {
    setHtml(document.body, `<p>page</p><div style="${parentStyle}"><iframe id="f" style="${frameStyle}"></iframe></div>`)
    const frame = document.getElementById('f')!
    const doc = sameOriginFrame(frame, '<p>FRAME-BODY</p><div id="host"></div>')
    setHtml(doc.getElementById('host')!.attachShadow({ mode: 'open' }), 'FRAME-SHADOW')
    // The inner document retains its own layout even when the embedding frame is hidden.
    layout((el) => el === frame && parentStyle === 'display:none')
    computedStyles([[frame.parentElement!, { contentVisibility: parentStyle === 'content-visibility:hidden' ? 'hidden' : 'visible' }]])
    expect(doc.body.getClientRects().length).toBeGreaterThan(0)
    const out = walker().read(null)
    expect(out.content).toContain('page')
    for (const text of ['FRAME-BODY', 'FRAME-SHADOW']) {
      if (shown) expect(out.content).toContain(text)
      else expect(out.content).not.toContain(text)
    }
    expect(out.skippedFrames).toBe(0)
    expect(out.deepRoots).toBe(0)
  })

  it('reads a shadow root whose host is display: contents, but not one under a hidden parent', () => {
    setHtml(document.body, '<p>page</p><x-article id="shown" style="display: contents"></x-article><div class="gone"><x-article id="hidden" style="display: contents"></x-article></div>')
    setHtml(document.getElementById('shown')!.attachShadow({ mode: 'open' }), '<p>ARTICLE-TEXT</p>')
    setHtml(document.getElementById('hidden')!.attachShadow({ mode: 'open' }), '<p>HIDDEN-TEXT</p>')
    layout((el) => el.classList.contains('gone'), contents)
    const out = walker().read(null)
    expect(out.content).toContain('ARTICLE-TEXT')
    expect(out.content).not.toContain('HIDDEN-TEXT')
  })

  it('reads a display: contents host nested directly inside another shadow root', () => {
    setHtml(document.body, '<p>page</p><div id="outer"></div>')
    const outer = document.getElementById('outer')!.attachShadow({ mode: 'open' })
    setHtml(outer, '<x-article id="inner" style="display: contents"></x-article>')
    setHtml(outer.getElementById('inner')!.attachShadow({ mode: 'open' }), '<p>NESTED-TEXT</p>')
    layout(() => false, contents)
    expect(walker().read(null).content).toContain('NESTED-TEXT')
  })

  it('leaves out a slottable no slot takes, even under a display: contents host', () => {
    setHtml(document.body, '<p>page</p><div id="outer"></div>')
    const outer = document.getElementById('outer')!.attachShadow({ mode: 'open' })
    setHtml(outer, '<x-article id="inner" style="display: contents">UNSLOTTED<span>UNSLOTTED-SPAN</span></x-article>')
    setHtml(outer.getElementById('inner')!.attachShadow({ mode: 'open' }), '<p>ARTICLE-TEXT</p>')
    layout(() => false, contents)
    const out = walker().read(null)
    expect(out.content).toContain('ARTICLE-TEXT')
    expect(out.content).not.toContain('UNSLOTTED')
  })

  it('cuts non-ASCII content by its JSON-escaped size and says truncated', () => {
    setHtml(document.body, `<p>${'字'.repeat(700000)}</p>`)
    const out = walker().read(null)
    expect(out.truncated).toBe(true)
    // Python's json.dumps writes each of these characters as \uXXXX: 6 bytes.
    expect(out.content!.length * 6).toBeLessThanOrEqual(3670016)
    expect(out.content!.length).toBeGreaterThan(600000)
  })

  it('says truncated when the page text is exactly at the cap and a frame has more', () => {
    setHtml(document.body, `<p>${'a'.repeat(2000000)}</p><iframe id="f"></iframe>`)
    sameOriginFrame(document.getElementById('f')!, '<p>FRAME-TEXT</p>')
    const out = walker().read(null)
    expect(out.content!.length).toBe(2000000)
    expect(out.truncated).toBe(true)
  })

  it('counts a cross-origin frame and an object document without reading them', () => {
    setHtml(document.body, '<p>page</p><iframe id="f"></iframe><object id="o"></object>')
    Object.defineProperty(document.getElementById('f')!, 'contentDocument', { get: () => null, configurable: true })
    sameOriginFrame(document.getElementById('o')!, '<p>OBJECT-TEXT</p>')
    const out = walker().read(null)
    expect(out.content).toContain('page')
    expect(out.content).not.toContain('OBJECT-TEXT')
    expect(out.skippedFrames).toBe(2)
  })

  it('counts shadow roots nested past the depth apart from skipped frames', () => {
    setHtml(document.body, '<p>page</p><div id="host"></div>')
    let host: Element = document.getElementById('host')!
    for (let i = 0; i < 10; i++) {
      const root = host.attachShadow({ mode: 'open' })
      setHtml(root, `<p>LEVEL-${i}</p><div class="h"></div>`)
      host = root.querySelector('.h')!
    }
    const out = walker().read(null)
    expect(out.content).toContain('LEVEL-0')
    expect(out.content).not.toContain('LEVEL-9')
    expect(out.skippedFrames).toBe(0)
    expect(out.deepRoots).toBeGreaterThanOrEqual(1)
  })

  it('reads only the element a ref names', () => {
    withBoxes()
    setHtml(document.body, '<p>outside</p><section><a href="/x">inside link</a><p>inside</p></section>')
    const api = walker()
    const link = api.snapshot().nodes.find((n) => n.role === 'link')!
    const out = api.read(link.ref!)
    expect(out.content).toContain('inside link')
    expect(out.content).not.toContain('outside')
  })

  it('answers ref_stale for a ref that is gone', () => {
    expect(walker().read('e999999')).toEqual({ ok: false, code: 'ref_stale' })
  })
})

describe('read, inline and block text', () => {
  function shadowText(html: string, whiteSpace = 'pre', display = 'block'): string {
    setHtml(document.body, '<div id="host"></div>')
    const root = document.getElementById('host')!.attachShadow({ mode: 'open' })
    setHtml(root, html)
    // Exercise the fallback below innerText as well as the shadow-root join.
    layout(() => false, () => true)
    computedStyles(Array.from(root.querySelectorAll('*'), (el): [Element, Partial<CSSStyleDeclaration>] => [el, {
      display: el.localName === 'p' || el.localName === 'pre' ? display : 'inline',
      whiteSpace: el.closest('pre') ? whiteSpace : 'normal',
    }]))
    return walker().read(null).content!
  }

  it.each([
    ['split token', '<p>tok<span>en-AAAA</span><span>BBBB</span></p>', 'token-AAAABBBB'],
    ['highlighted command', '<pre><code><span class="k">npm</span> <span>run</span> <span>build</span></code></pre>', 'npm run build'],
    ['indented code', '<pre><code>  npm install\n    npm run build</code></pre>', '  npm install\n    npm run build'],
    ['paragraphs', '<p>first</p><p>second</p>', 'first\nsecond'],
    ['explicit break', 'a<br>b', 'a\nb'],
    ['inline whitespace', '<p>  one \t<span> \n two</span>   three  </p>', 'one two three'],
    ['nested block edges', '<p> first </p> \n <p> second </p>', 'first\nsecond'],
    ['blank separators', 'a<br><br><br><br>b', 'a\n\nb'],
  ])('preserves %s in an open shadow root', (_name, html, expected) => {
    expect(shadowText(html)).toBe(expected)
  })

  it.each(['pre', 'pre-wrap', 'pre-line', 'break-spaces'])('keeps preformatted whitespace for %s', (whiteSpace) => {
    expect(shadowText('<pre>  first\n\n\n    second</pre>', whiteSpace)).toBe('  first\n\n\n    second')
  })

  it.each(['block', 'list-item', 'table', 'table-row', 'table-caption', 'flow-root', 'flex', 'grid'])('separates %s blocks', (display) => {
    expect(shadowText('<p>first</p><p>second</p>', 'pre', display)).toBe('first\nsecond')
  })

  it.each(['flex', 'grid'])('separates inline %s items', (display) => {
    expect(shadowText('<p><span>first</span><span>second</span></p>', 'pre', display)).toBe('first\nsecond')
  })

  it.each(['inline', 'inline-block', 'inline-flex', 'inline-grid', 'contents'])('joins %s siblings without a separator', (display) => {
    expect(shadowText('<p>tok</p><p>en</p>', 'pre', display)).toBe('token')
  })

  it('separates table cells with whitespace', () => {
    expect(shadowText('<p>first</p><p>second</p>', 'pre', 'table-cell')).toBe('first second')
  })
})

describe('read, text-node visibility', () => {
  it('drops visibility:hidden SVG text but keeps a descendant that restores visibility', () => {
    setHtml(document.body, '<div id="host"></div>')
    const root = document.getElementById('host')!.attachShadow({ mode: 'open' })
    setHtml(root, '<svg><text id="hidden">HIDDEN-LABEL<tspan id="inherited">HIDDEN-INHERITED</tspan>' +
      '<tspan>SHOWN-LABEL</tspan></text></svg>')
    for (const el of root.querySelectorAll('*')) {
      Object.defineProperty(el, 'innerText', { configurable: true, value: undefined })
    }
    computedStyles([
      [root.getElementById('hidden')!, { visibility: 'hidden' }],
      [root.getElementById('inherited')!, { visibility: 'hidden' }],
    ])
    const out = walker().read(null)
    expect(out.content).not.toContain('HIDDEN-')
    expect(out.content).toContain('SHOWN-LABEL')
  })

  it('drops direct shadow text under a display:none ancestor', () => {
    setHtml(document.body, '<div id="hidden"><div id="host"></div></div><div id="shown"></div>')
    setHtml(document.getElementById('host')!.attachShadow({ mode: 'open' }), 'HIDDEN-SHADOW')
    setHtml(document.getElementById('shown')!.attachShadow({ mode: 'open' }), 'SHOWN-SHADOW')
    // Keep range rects present so this proves the ancestor check independently.
    computedStyles([[document.getElementById('hidden')!, { display: 'none' }]])
    const out = walker().read(null)
    expect(out.content).not.toContain('HIDDEN-SHADOW')
    expect(out.content).toContain('SHOWN-SHADOW')
  })

  it('drops display:contents text under a content-visibility:hidden ancestor', () => {
    setHtml(document.body, '<div id="host"></div>')
    const root = document.getElementById('host')!.attachShadow({ mode: 'open' })
    setHtml(root, '<section id="hidden"><x-article>HIDDEN-CONTENTS</x-article></section>' +
      '<x-article>SHOWN-CONTENTS</x-article>')
    layout(() => false, (el) => contents(el) || el.id === 'hidden')
    computedStyles([
      [root.getElementById('hidden')!, { contentVisibility: 'hidden' }],
      ...Array.from(root.querySelectorAll('x-article'), (el): [Element, Partial<CSSStyleDeclaration>] =>
        [el, { display: 'contents' }]),
    ])
    const out = walker().read(null)
    expect(out.content).not.toContain('HIDDEN-CONTENTS')
    expect(out.content).toContain('SHOWN-CONTENTS')
  })

  it('drops closed details bodies but keeps their first summary and open bodies', () => {
    setHtml(document.body, '<div id="host"></div>')
    const root = document.getElementById('host')!.attachShadow({ mode: 'open' })
    setHtml(root, '<details><div>HIDDEN-BEFORE</div><summary><span>SHOWN-SUMMARY</span></summary>' +
      '<summary>HIDDEN-SECOND-SUMMARY</summary><p>HIDDEN-BODY</p>' +
      '<details open><summary>HIDDEN-NESTED-SUMMARY</summary></details></details>' +
      '<details open><summary>SHOWN-OPEN-SUMMARY</summary><p>SHOWN-OPEN-BODY</p></details>')
    // Force child-by-child reads; nonempty ranges alone do not prove visibility.
    layout(() => false, () => true)
    const out = walker().read(null)
    expect(out.content).not.toContain('HIDDEN-')
    expect(out.content).toContain('SHOWN-SUMMARY')
    expect(out.content).toContain('SHOWN-OPEN-SUMMARY')
    expect(out.content).toContain('SHOWN-OPEN-BODY')
  })
})

describe('read, protected custom fields', () => {
  const fields = [
    'contenteditable="true" role="textbox" id="otp"',
    'contenteditable="true" role="textbox" data-kc-asked',
    'contenteditable id="otp"',
    'contenteditable="plaintext-only" autocomplete="one-time-code"',
    'role="textbox" id="otp"',
    'role="searchbox" id="otp"',
    'role="combobox" id="otp"',
    'role="spinbutton" id="otp"',
  ]
  const prose = (attrs: string): string =>
    `<section><p>Before the field</p><div ${attrs}><span>OTP-123456</span></div>` +
    '<p>After the field</p><div contenteditable="true">ordinary editable words</div></section>'

  it.each(fields)('text omits protected values in light, shadow and frame prose: %s', (attrs) => {
    setHtml(document.body, prose(attrs) + '<div id="host"></div><iframe id="f"></iframe>')
    const shadow = document.getElementById('host')!.attachShadow({ mode: 'open' })
    setHtml(shadow, prose(attrs))
    sameOriginFrame(document.getElementById('f')!, prose(attrs))
    const before = document.body.outerHTML
    const out = walker().read(null)
    expect(out.ok).toBe(true)
    expect(out.content).not.toContain('OTP-123456')
    expect(out.content!.match(/Before the field/g)).toHaveLength(3)
    expect(out.content!.match(/After the field/g)).toHaveLength(3)
    expect(out.content!.match(/ordinary editable words/g)).toHaveLength(3)
    expect(out.content).toMatch(/Before the field\s+After the field\s+ordinary editable words/)
    expect(document.body.outerHTML).toBe(before)
  })

  it('omits light-DOM text assigned to a protected shadow field', () => {
    setHtml(document.body, '<p>kept prose</p><div id="host">OTP-123456</div>')
    const host = document.getElementById('host')!
    const shadow = host.attachShadow({ mode: 'open' })
    setHtml(shadow, '<div role="textbox" data-kc-asked><slot></slot></div>')
    // happy-dom does not connect a text node to its assigned slot.
    Object.defineProperty(host.firstChild!, 'assignedSlot', { get: () => shadow.querySelector('slot') })
    const out = walker().read(null)
    expect(out.content).toContain('kept prose')
    expect(out.content).not.toContain('OTP-123456')
    expect(host.textContent).toBe('OTP-123456')
  })

  it('does not read a protected field through its shadow root or a descendant ref', () => {
    setHtml(document.body, '<p>kept prose</p><div role="textbox" data-kc-asked><div id="host"></div><a href="/x" aria-label="child">OTP-123456</a></div>')
    setHtml(document.getElementById('host')!.attachShadow({ mode: 'open' }), '<span>SHADOW-SECRET</span>')
    withBoxes()
    const api = walker()
    const ref = api.snapshot().nodes.find((n) => n.name === 'child')!.ref!
    expect(api.read(null).content).toBe('kept prose')
    expect(api.read(ref).content).not.toContain('OTP-123456')
  })
})

describe('read, a reading-heavy page', () => {
  // A tutorial page: headings, long prose, a code block, an inactive tab panel, a
  // collapsed section and a same-origin frame. `snapshot` lists only controls and
  // headings, so the prose and the code never reach it; `read` returns them.
  const PAGE =
    '<h1>Lab 2: Deploy the sample app</h1>' +
    '<p>In this lab you build the sample service and run it on your own machine.</p>' +
    '<pre><code>npm install\nnpm run build -- --mode=lab</code></pre>' +
    '<div role="tablist"><button role="tab" aria-selected="true">macOS</button>' +
    '<button role="tab" aria-selected="false">Windows</button></div>' +
    '<div role="tabpanel"><p>Open Terminal and run the commands above.</p></div>' +
    '<div role="tabpanel" class="gone"><p>INACTIVE-TAB-TEXT</p></div>' +
    '<details><summary>Troubleshooting</summary><p>COLLAPSED-BODY</p></details>' +
    '<iframe id="f" title="Quiz"></iframe>'

  it('returns the prose, the code block and the frame text that snapshot leaves out', () => {
    setHtml(document.body, PAGE)
    sameOriginFrame(document.getElementById('f')!, '<p>Question 1: which command builds the app?</p>')
    layout((el) => el.classList.contains('gone') || (el.localName === 'p' && !!el.closest('details:not([open])')))
    withBoxes()
    const api = walker()
    const outline = api.snapshot().nodes.map((n) => n.name).join('\n')
    expect(outline).toContain('Lab 2: Deploy the sample app')
    expect(outline).not.toContain('In this lab you build')
    expect(outline).not.toContain('npm run build')

    const out = api.read(null)
    expect(out.ok).toBe(true)
    expect(out.content).toContain('Lab 2: Deploy the sample app')
    expect(out.content).toContain('In this lab you build the sample service')
    expect(out.content).toContain('npm run build -- --mode=lab')
    expect(out.content).toContain('Open Terminal and run the commands above.')
    expect(out.content).toContain('Troubleshooting')
    expect(out.content).toContain('Question 1: which command builds the app?')
    expect(out.skippedFrames).toBe(0)
  })
})
