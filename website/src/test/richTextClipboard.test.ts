import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { markdownToCleanHtml, rangeToCleanHtml } from '../components/markdown/richTextClipboard'
import { copyRichToClipboard } from '../utils/clipboard'
import { holdVisibleCodeText } from '../utils/visibleText'
import { setHtml } from './domFixture'

describe('markdownToCleanHtml', () => {
  it('keeps headings, lists, emphasis, code, quotes and links as semantic tags', () => {
    const html = markdownToCleanHtml([
      '## Title', '', 'Some *em*, **strong**, ~~gone~~ and `code`.', '',
      '1. first', '2. second', '', '> quoted', '', '```ts', 'const x = 1', '```', '',
      '[site](https://example.com)',
    ].join('\n'))
    expect(html).toContain('<h2>Title</h2>')
    expect(html).toContain('<em>em</em>')
    expect(html).toContain('<strong>strong</strong>')
    expect(html).toContain('<del>gone</del>')
    expect(html).toContain('<code>code</code>')
    expect(html).toMatch(/<ol>\s*<li>first<\/li>/)
    expect(html).toMatch(/<blockquote>\s*<p>quoted<\/p>/)
    expect(html).toContain('<pre><code>const x = 1\n</code></pre>')
    expect(html).toContain('<a href="https://example.com">site</a>')
  })

  it('renders GFM tables with alignment and no classes or styles', () => {
    const html = markdownToCleanHtml('| a | b |\n|:--|--:|\n| 1 | 2 |')
    expect(html).toContain('<table>')
    expect(html).toContain('<th align="left">a</th>')
    expect(html).toContain('<td align="right">2</td>')
    expect(html).not.toMatch(/class=|style=/)
  })

  it('never passes raw HTML through as markup, but keeps its source as text', () => {
    const html = markdownToCleanHtml('<div style="border:1px solid">x</div>\n\nhi <SCRIPT>alert(1)</SCRIPT> there <b onclick="x()">b</b> and List<String>')
    // Judged on the parsed fragment, not by pattern: any element or attribute
    // the converter does not emit itself is a failure.
    const doc = new DOMParser().parseFromString(html, 'text/html')
    expect(Array.from(doc.body.querySelectorAll('*')).map(e => e.tagName)).toEqual(['P'])
    expect(doc.body.querySelector('[style], [onclick]')).toBeNull()
    expect(doc.body.textContent).toContain('alert(1)')
    expect(doc.body.textContent).toContain('List<String>')
  })

  it('turns a raw <br> into a line break and drops HTML comments', () => {
    const html = markdownToCleanHtml('one<br>two <!-- note -->')
    expect(html).toMatch(/one<br>\s*two/)
    expect(html).not.toContain('note')
  })

  it('unwraps links that are not absolute http(s) or mailto', () => {
    const html = markdownToCleanHtml('[bad](javascript:alert(1)) [rel](./src/a.ts) [mail](mailto:a@b.c)')
    expect(html).not.toContain('javascript:')
    expect(html).not.toContain('./src/a.ts')
    expect(html).toContain('bad')
    expect(html).toContain('rel')
    expect(html).toContain('<a href="mailto:a@b.c">mail</a>')
  })

  it('never emits an image: a remote one becomes a link, a relative one its alt text', () => {
    const remote = markdownToCleanHtml('![chart](https://example.com/c.png)')
    expect(remote).not.toContain('<img')
    expect(remote).toContain('<a href="https://example.com/c.png">chart</a>')
    const rel = markdownToCleanHtml('![local shot](shots/a.png)')
    expect(rel).not.toMatch(/<img|<a /)
    expect(rel).toContain('local shot')
  })

  it('a linked image becomes the link label, never a nested link', () => {
    const html = markdownToCleanHtml('[![logo](https://x.example/i.png)](https://y.example/page)')
    expect(html).toContain('<a href="https://y.example/page">logo</a>')
    expect(html).not.toContain('x.example')
  })

  it('drops the screen-reader-only footnote heading', () => {
    const html = markdownToCleanHtml('Claim.[^1]\n\n[^1]: Source.')
    expect(html).not.toContain('Footnotes')
    expect(html).toContain('Source.')
  })

  it('caps pathological indentation the way the chat renderer does', () => {
    const html = markdownToCleanHtml(`${' '.repeat(100_000)}deep`)
    expect(html).toContain('deep')
  })

  it('keeps task-list checkboxes but no other inputs', () => {
    const html = markdownToCleanHtml('- [x] done\n- [ ] todo')
    expect(html).toContain('<input type="checkbox" checked disabled>')
    expect(html).not.toMatch(/class=/)
  })
})

describe('rangeToCleanHtml', () => {
  let root: HTMLElement
  beforeEach(() => { root = document.createElement('div'); document.body.append(root) })
  afterEach(() => root.remove())

  const between = (start: Node, startOffset: number, end: Node, endOffset: number) => {
    const range = document.createRange()
    range.setStart(start, startOffset)
    range.setEnd(end, endOffset)
    return range
  }
  const text = (selector: string, last = false) => {
    const el = root.querySelector(selector)!
    return (last ? el.lastChild : el.firstChild)!
  }

  it('writes only the selected part, with the structure a selection across blocks needs', () => {
    setHtml(root, '<p class="mb-2">Intro with <strong class="font-bold">bold</strong> and <a class="chip" href="https://example.com/docs">docs</a>.</p><ul><li>first item</li><li>second item</li></ul>')
    const html = rangeToCleanHtml(root, between(text('p'), 6, text('li:last-child'), 6))
    expect(html).toBe('<p>with <strong>bold</strong> and <a href="https://example.com/docs">docs</a>.</p><ul><li>first item</li><li>second</li></ul>')
  })

  it('keeps an empty table cell, so the cells after it stay in their columns', () => {
    setHtml(root, '<table><thead><tr><th></th><th>Option A</th></tr></thead><tbody><tr><td>Cost</td><td>low</td></tr></tbody></table>')
    expect(rangeToCleanHtml(root, between(root, 0, text('td:last-child'), 3)))
      .toBe('<table><thead><tr><th></th><th>Option A</th></tr></thead><tbody><tr><td>Cost</td><td>low</td></tr></tbody></table>')
  })

  it('drops a filled table cell the selection only touches at its edge', () => {
    setHtml(root, '<p>Intro</p><table><tbody><tr><td>Cost</td><td>low</td></tr></tbody></table>')
    const firstCell = root.querySelector('td')!
    expect(rangeToCleanHtml(root, between(text('p'), 0, firstCell, 0))).toBe('Intro')
    expect(rangeToCleanHtml(root, between(text('td'), 0, root.querySelector('td:last-child')!, 0))).toBe('Cost')
  })

  it('keeps a blank cell for each column before a selection that starts mid-row, so the selected cells stay under their headers', () => {
    setHtml(root, '<table><thead><tr><th>Item</th><th>Price</th></tr></thead><tbody><tr><td>Book</td><td>10</td></tr></tbody></table>')
    expect(rangeToCleanHtml(root, between(text('th:last-child'), 0, text('td:last-child'), 2)))
      .toBe('<table><thead><tr><th></th><th>Price</th></tr></thead><tbody><tr><td>Book</td><td>10</td></tr></tbody></table>')
  })

  it('a selection inside a later cell of one row pastes as its inline content', () => {
    setHtml(root, '<table><tbody><tr><td>Book</td><td>ten dollars</td></tr></tbody></table>')
    expect(rangeToCleanHtml(root, between(text('td:last-child'), 0, text('td:last-child'), 3))).toBe('ten')
  })

  it('copies an unfurled link card as its URL, never its fetched title and description', () => {
    setHtml(root, '<p>See <a href="https://example.com/a" data-unfurl-url="https://example.com/a"><span class="block">Title</span><span class="block">Desc</span></a> now</p>')
    expect(rangeToCleanHtml(root, between(text('p'), 0, text('p', true), 4)))
      .toBe('See <a href="https://example.com/a">https://example.com/a</a> now')
    expect(rangeToCleanHtml(root, between(text('span'), 1, text('span'), 3)))
      .toBe('<a href="https://example.com/a">https://example.com/a</a>')
  })

  it('keeps the numbers the reader saw when the selection starts past the first list item', () => {
    setHtml(root, '<ol><li>one</li><li>two</li><li>three</li></ol><ol start="4"><li>four</li><li>five</li></ol>')
    expect(rangeToCleanHtml(root, between(text('li:nth-child(2)'), 0, text('li:nth-child(3)'), 5)))
      .toBe('<ol start="2"><li>two</li><li>three</li></ol>')
    expect(rangeToCleanHtml(root, between(text('ol:last-child li:last-child'), 0, text('ol:last-child li:last-child'), 4)))
      .toBe('five')
    expect(rangeToCleanHtml(root, between(text('li:nth-child(3)'), 0, text('ol:last-child li:last-child'), 4)))
      .toBe('<ol start="3"><li>three</li></ol><ol start="4"><li>four</li><li>five</li></ol>')
  })

  it('keeps cell spans, also on a blank cell kept for alignment', () => {
    setHtml(root, '<table><tbody><tr><td rowspan="2">Region A</td><td>10</td></tr><tr><td colspan="2">20</td></tr></tbody></table>')
    expect(rangeToCleanHtml(root, between(text('td:nth-child(2)'), 0, text('tr:last-child td'), 2)))
      .toBe('<table><tbody><tr><td rowspan="2"></td><td>10</td></tr><tr><td colspan="2">20</td></tr></tbody></table>')
  })

  it('a selection inside one block pastes as its inline content', () => {
    setHtml(root, '<ul><li><p>Use <code>npm test</code> to check</p></li></ul>')
    expect(rangeToCleanHtml(root, between(text('p'), 0, text('p', true), 3))).toBe('Use <code>npm test</code> to')
  })

  it('reads the selection as the reader sees it: no hidden text or buttons, a code block its code, a formula its TeX, a closed details its summary', () => {
    setHtml(root, '<p>Run<span class="sr-only"> secretly</span> this<button>Copy</button>:</p>'
      + '<div class="code-block"><span>sh</span><pre>highlighted</pre></div>'
      + '<details><summary>Steps</summary>folded body</details>'
      + '<p>then <span class="katex"><span class="katex-mathml"><math><semantics><mi>x</mi><annotation encoding="application/x-tex">x^2</annotation></semantics></math></span><span class="katex-html" aria-hidden="true">x2</span></span> ends</p>')
    const release = holdVisibleCodeText(root.querySelector('.code-block')!, 'npm test')
    try {
      expect(rangeToCleanHtml(root, between(text('p'), 0, text('p:last-child', true), 5))).toBe('<p>Run this:</p><pre><code>npm test</code></pre>Steps<p>then x^2 ends</p>')
    } finally {
      release()
    }
  })

  it('writes nothing when the selection starts or ends inside a code block, whose drawn code it cannot read', () => {
    setHtml(root, '<p>Before</p><div class="code-block"><span>sh</span><div class="pierre-surface"></div></div>')
    const release = holdVisibleCodeText(root.querySelector('.code-block')!, 'npm test')
    try {
      expect(rangeToCleanHtml(root, between(text('p'), 2, root.querySelector('.pierre-surface')!, 0))).toBe('')
    } finally {
      release()
    }
  })
})

describe('copyRichToClipboard', () => {
  const realItem = (globalThis as { ClipboardItem?: unknown }).ClipboardItem
  afterEach(() => {
    vi.unstubAllGlobals()
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    ;(globalThis as { ClipboardItem?: unknown }).ClipboardItem = realItem
  })
  beforeEach(() => { vi.clearAllMocks() })

  it('writes text/html and text/plain through the async Clipboard API', async () => {
    const items: Record<string, Blob>[] = []
    vi.stubGlobal('ClipboardItem', class { constructor(data: Record<string, Blob>) { items.push(data) } })
    const write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { write }, configurable: true })
    await expect(copyRichToClipboard('<p>hi</p>', 'hi')).resolves.toBe(true)
    expect(write).toHaveBeenCalledTimes(1)
    expect(Object.keys(items[0]).sort()).toEqual(['text/html', 'text/plain'])
    expect(await items[0]['text/html'].text()).toBe('<p>hi</p>')
    expect(await items[0]['text/plain'].text()).toBe('hi')
  })

  it('falls back to the copy event with both flavours when the async write is unavailable', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    const setData = vi.fn()
    const execCommand = vi.fn(() => {
      const ev = new Event('copy', { bubbles: true, cancelable: true }) as Event & { clipboardData: { setData: typeof setData } }
      ev.clipboardData = { setData }
      document.body.dispatchEvent(ev)
      return true
    })
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true })
    await expect(copyRichToClipboard('<p>hi</p>', 'hi')).resolves.toBe(true)
    expect(setData).toHaveBeenCalledWith('text/plain', 'hi')
    expect(setData).toHaveBeenCalledWith('text/html', '<p>hi</p>')
  })

  it('falls back when the async write is refused, and reports a total failure as false', async () => {
    vi.stubGlobal('ClipboardItem', class { constructor(_d: unknown) {} })
    Object.defineProperty(navigator, 'clipboard', { value: { write: vi.fn().mockRejectedValue(new Error('denied')) }, configurable: true })
    Object.defineProperty(document, 'execCommand', { value: vi.fn().mockReturnValue(false), configurable: true })
    await expect(copyRichToClipboard('<p>hi</p>', 'hi')).resolves.toBe(false)
    expect(document.execCommand).toHaveBeenCalledWith('copy')
  })
})
