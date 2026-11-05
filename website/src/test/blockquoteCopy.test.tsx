import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

describe('blockquote copy button', () => {
  const realItem = (globalThis as { ClipboardItem?: unknown }).ClipboardItem
  const realExec = Object.getOwnPropertyDescriptor(document, 'execCommand')
  afterEach(() => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    ;(globalThis as { ClipboardItem?: unknown }).ClipboardItem = realItem
    if (realExec) Object.defineProperty(document, 'execCommand', realExec)
    else delete (document as { execCommand?: unknown }).execCommand
  })

  function stubClipboard() {
    const items: Record<string, Blob>[] = []
    ;(globalThis as { ClipboardItem?: unknown }).ClipboardItem = class { constructor(data: Record<string, Blob>) { items.push(data) } }
    const write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { write }, configurable: true })
    return { items, write }
  }

  it('copies the quote as clean HTML with live links and a plain-text fallback', async () => {
    const { items, write } = stubClipboard()
    const { getAllByTestId } = render(
      <MarkdownRenderer content={'Intro\n\n> See [CR-1](https://example.com/cr/1) and **this**.\n\nAfter'} />,
    )
    fireEvent.click(getAllByTestId('blockquote-copy')[0])
    await waitFor(() => expect(write).toHaveBeenCalledTimes(1))
    const html = await items[0]['text/html'].text()
    expect(html).toContain('<a href="https://example.com/cr/1">CR-1</a>')
    expect(html).toContain('<strong>this</strong>')
    expect(html).not.toMatch(/class=|style=|blockquote|Intro|After/)
    expect(await items[0]['text/plain'].text()).toBe('See [CR-1](https://example.com/cr/1) and **this**.')
  })

  async function copyOf(content: string, props: Record<string, unknown> = {}) {
    const { items, write } = stubClipboard()
    const view = render(<MarkdownRenderer content={content} {...props} />)
    fireEvent.click(view.getAllByTestId('blockquote-copy')[0])
    await waitFor(() => expect(write).toHaveBeenCalledTimes(1))
    return { view, html: await items[0]['text/html'].text(), plain: await items[0]['text/plain'].text() }
  }

  it('resolves reference links from the full document', async () => {
    const { html, plain } = await copyOf('> See [CR-1][cr].\n\n[cr]: https://example.com/cr/1\n[other]: https://example.com/other')
    expect(html).toContain('<a href="https://example.com/cr/1">CR-1</a>')
    expect(plain).toBe('See [CR-1](https://example.com/cr/1).')
  })

  it('keeps the first reference definition when the quote repeats it', async () => {
    const { html, plain } = await copyOf(
      '[ref]: https://first.example\n\n> [link][ref]\n>\n> [ref]: https://second.example',
    )
    expect(html).toContain('<a href="https://first.example">link</a>')
    expect(html).not.toContain('second.example')
    expect(plain).toBe('[link](https://first.example)')
  })

  it('copies tab-prefixed quote content as ordinary markdown', async () => {
    const { html, plain } = await copyOf('>\t[link](https://example.com)')
    expect(html).toBe('<p><a href="https://example.com">link</a></p>')
    expect(plain).toBe('[link](https://example.com)')
  })

  it('includes footnote definitions used by the quote', async () => {
    const { html, plain } = await copyOf('> Claim.[^1]\n\n[^1]: Source.')
    expect(html).toContain('Claim.')
    expect(html).toContain('Source.')
    expect(plain).toBe('Claim.[^1]\n\n[^1]: Source.')
  })

  it('includes transitive footnotes once when definitions cycle', async () => {
    const { plain } = await copyOf('> Claim.[^1]\n\n[^1]: First.[^2]\n\n[^2]: Second.[^1]')
    expect(plain.match(/^\[\^1\]:/gm)).toHaveLength(1)
    expect(plain.match(/^\[\^2\]:/gm)).toHaveLength(1)
  })

  it('keeps the period after a session ref', async () => {
    const { html, plain } = await copyOf('> Continue in `chat-1380-1789049480`.')
    expect(html).toContain('<code>chat-1380-1789049480</code>.')
    expect(plain).toBe('Continue in `chat-1380-1789049480`.')
  })

  it('keeps a redaction placeholder and the query tail glued to it', async () => {
    const host = 'my-bucket.s3.us-east-1.amazonaws.com'
    const ph = `[REDACTED: suspicious URL to ${host}]`
    const record = { domain: host, rule: 'exfil_hard_credential', path: '/a', query_chars: 9, url: null, url_withheld: 'credential' }
    const { view, html, plain } = await copyOf(`> See ${ph}?token=abc now`, { blockedLinks: [record], slotKey: 's1' })
    expect(view.getByTestId('blocked-link-chip')).toBeTruthy()
    expect(html).toContain(`${ph}?token=abc now`)
    expect(plain).toBe(`See ${ph}?token=abc now`)
  })

  it('adds no backslash escapes to the plain text', async () => {
    const { html, plain } = await copyOf('> pick [option] now, 2 * 3, AT&T')
    expect(plain).toBe('pick [option] now, 2 * 3, AT&T')
    expect(new DOMParser().parseFromString(html, 'text/html').body.textContent).toBe(plain)
  })

  it('copies quoted math once', async () => {
    const { html, plain } = await copyOf('> The area is $$\\pi r^2$$')
    expect(html.match(/pi r\^2/g)).toHaveLength(1)
    expect(html).not.toMatch(/math|katex|annotation/i)
    expect(plain).toBe('The area is $$\\pi r^2$$')
  })

  it.each([
    ['> set $$\\{x\\}$$', 'set $$\\{x\\}$$'],
    ['> $$a \\\\ b$$', '$$a \\\\ b$$'],
    ['> inline \\(x^2\\)', 'inline \\(x^2\\)'],
  ])('keeps backslash escapes inside quoted math: %s', async (content, expected) => {
    expect((await copyOf(content)).plain).toBe(expected)
  })

  it('copies the inner markdown of nested and lazy quote lines', async () => {
    const { html, plain } = await copyOf('> outer\nlazy\n>\n> > inner')
    expect(plain).toBe('outer\nlazy\n\n> inner')
    expect(html).toBe('<p>outer\nlazy</p>\n<blockquote>\n<p>inner</p>\n</blockquote>')
  })

  it('leaves table action buttons out of the plain text', async () => {
    const { items, write } = stubClipboard()
    const { getAllByTestId } = render(<MarkdownRenderer content={'> Intro\n>\n> | a | b |\n> |---|---|\n> | 1 | 2 |'} />)
    fireEvent.click(getAllByTestId('blockquote-copy')[0])
    await waitFor(() => expect(write).toHaveBeenCalledTimes(1))
    const plain = await items[0]['text/plain'].text()
    expect(plain).toBe('Intro\n\n| a | b |\n| - | - |\n| 1 | 2 |')
    expect(plain).not.toMatch(/Copy|CSV|Widen/)
  })

  it('gives a nested quote no button of its own', () => {
    const { getAllByTestId } = render(<MarkdownRenderer content={'> outer\n>\n> > inner'} />)
    expect(getAllByTestId('blockquote-copy')).toHaveLength(1)
  })

  it('gives a raw-HTML quote no button, but a markdown quote inside it one', async () => {
    const raw = render(<MarkdownRenderer content={'<blockquote>hi</blockquote>'} />)
    expect(raw.container.querySelector('blockquote')?.textContent).toBe('hi')
    expect(raw.queryByTestId('blockquote-copy')).toBeNull()
    raw.unmount()

    const { plain } = await copyOf('<blockquote>\n\n> inner\n\n</blockquote>')
    expect(plain).toBe('inner')
  })

  it('shows an error until a later copy succeeds', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    const exec = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true)
    Object.defineProperty(document, 'execCommand', { value: exec, configurable: true })
    const { getByTestId, findByText, queryByText } = render(<MarkdownRenderer content={'> quote'} />)
    const button = getByTestId('blockquote-copy')

    fireEvent.click(button)
    expect(await findByText('Copy failed. Select the quote and copy it yourself.')).toBeTruthy()
    expect(button.getAttribute('aria-label')).toBe('Copy quote')

    fireEvent.click(button)
    await waitFor(() => expect(button.getAttribute('aria-label')).toBe('Copied!'))
    expect(exec).toHaveBeenCalledTimes(2)
    expect(queryByText('Copy failed. Select the quote and copy it yourself.')).toBeNull()
  })
})
