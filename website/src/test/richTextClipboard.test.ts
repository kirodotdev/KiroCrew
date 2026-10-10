import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { markdownToCleanHtml } from '../components/markdown/richTextClipboard'
import { copyRichToClipboard } from '../utils/clipboard'

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
