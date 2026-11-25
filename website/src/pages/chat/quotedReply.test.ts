import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest'
import type { ChatMessage } from '../../types'
import { installHighlightApiStub, registeredRanges } from '../../test/highlightApiStub'
import { MAX_INDENT_COLS, capWhitespaceRuns } from '../../utils/markdownDepthBound'
import { findQuotedReply, quoteMarkRef, quotedTextRange } from './quotedReply'

vi.mock('../../utils/markdownDepthBound', async importOriginal => {
  const actual = await importOriginal<typeof import('../../utils/markdownDepthBound')>()
  return { ...actual, capWhitespaceRuns: vi.fn(actual.capWhitespaceRuns) }
})

const row = (role: string, ts: string, content: string) => ({ role, content, ts, cls: '' }) as unknown as ChatMessage

describe('findQuotedReply', () => {
  // What selecting "Run the CLI" and "Done" in the rendered list puts in the
  // draft: the list numbers and the link's address were never on screen.
  const quoted = '> Run the CLI\n> Done'
  const listReply = row('assistant', 't2', '1. Run the [CLI](https://example.com/cli)\n2. Done')

  it('finds the nearest earlier reply by its rendered words, across list numbers, emphasis and links', () => {
    const messages = [row('assistant', 't1', 'Unrelated'), listReply, row('user', 't3', `${quoted}\n\nwhy?`)]
    expect(findQuotedReply(messages, quoted, 2)).toBe(listReply)
    expect(findQuotedReply([row('assistant', 't1', 'Run **the** `CLI`\n\nDone')], quoted, 1)?.ts).toBe('t1')
  })

  it('never matches the quoting message, a later reply, or a user message', () => {
    const messages = [
      row('user', 't1', 'Run the CLI Done'),
      row('user', 't2', `${quoted}\n\nwhy?`),
      row('assistant', 't3', 'Run the CLI, Done: as you quoted'),
    ]
    expect(findQuotedReply(messages, quoted, 1)).toBeNull()
  })

  it('does not match on a link address or a raw HTML tag the reply never draws', () => {
    expect(findQuotedReply([row('assistant', 't1', 'See [docs](https://example.com/cli)')], '> examplecom', 1)).toBeNull()
    expect(findQuotedReply([row('assistant', 't1', 'a<br>b')], '> br', 1)).toBeNull()
  })

  it('skips a newer reply whose only match is the TeX inside a formula', () => {
    const prose = row('assistant', 't1', 'Take the left door.')
    for (const formula of ['The bracket:\n\n$$\n\\left(x\\right)\n$$', 'So \\(\\left(x\\right)\\) holds.']) {
      const messages = [prose, row('assistant', 't2', formula), row('user', 't3', '> left\n\nwhy?')]
      expect(findQuotedReply(messages, '> left', 2)).toBe(prose)
    }
  })

  it('prefers whole words, so a newer reply holding the quote inside a longer word is skipped', () => {
    const older = row('assistant', 't1', 'Set the port to 8080')
    const messages = [older, row('assistant', 't2', 'This is important'), row('user', 't3', '> port\n\nwhy?')]
    expect(findQuotedReply(messages, '> port', 2)).toBe(older)
    const both = document.createElement('p')
    both.textContent = 'This is important. Set the port to 8080'
    const range = quotedTextRange(both, '> port')
    expect(range?.startContainer).toBe(both.firstChild)
    expect(range?.startOffset).toBe('This is important. Set the '.length)
  })

  it('falls back to letters and digits alone, for a selection cut mid-word or CJK text with no spaces', () => {
    const cases = [
      ['Run the CLI setup with demo init', 'un the CLI setup with demo ini'],
      ['请先运行命令行工具，然后登录', '运行命令'],
    ]
    for (const [reply, selected] of cases) {
      const source = row('assistant', 't1', reply)
      expect(findQuotedReply([source], `> ${selected}`, 1)).toBe(source)
      const el = document.createElement('p')
      el.textContent = reply
      expect(quotedTextRange(el, `> ${selected}`)?.toString()).toBe(selected)
    }
  })

  it('offers nothing for a quote with no letters or digits', () => {
    expect(findQuotedReply([row('assistant', 't1', '---')], '> ---', 1)).toBeNull()
  })

  // The cap is what keeps a deeply indented reply cheap to parse; a list deep
  // enough to show that by time is too slow for the shared CI suite.
  it('reads a reply through the whitespace cap the bubble renders it with', () => {
    const deep = row('assistant', 't1', ['- item', ' '.repeat(MAX_INDENT_COLS + 44) + '- item', '', 'Run the CLI', 'Done'].join('\n'))
    expect(findQuotedReply([deep], quoted, 1)).toBe(deep)
    expect(capWhitespaceRuns).toHaveBeenCalledWith(deep.content)
  })
})

function bubble(html: string): HTMLElement {
  const el = document.createElement('div')
  el.replaceChildren(...new DOMParser().parseFromString(html, 'text/html').body.childNodes)
  document.body.appendChild(el)
  return el
}

describe('quotedTextRange', () => {
  afterEach(() => { document.body.replaceChildren() })

  it('spans the quoted words across element boundaries and skips chrome', () => {
    const el = bubble('<span data-message-search-skip>Run the</span><ol><li>Run <strong>the</strong> <a>CLI</a></li><li>Done</li></ol>')
    const range = quotedTextRange(el, '> Run the CLI\n> Done')
    expect(range?.startContainer.textContent).toBe('Run ')
    expect(range?.toString()).toBe('Run the CLIDone')
  })

  it('is null when the words are not in the text', () => {
    expect(quotedTextRange(bubble('<p>Something else</p>'), '> Run the CLI')).toBeNull()
  })
})

describe('quoteMarkRef', () => {
  beforeAll(() => { installHighlightApiStub() })
  afterEach(() => { document.body.replaceChildren() })

  it('marks the words inside the bubble while attached, and unmarks them on release', () => {
    const row = bubble('<span>Kiro Crew</span><div class="message-bubble"><p>Install Kiro Crew first</p></div>')
    const ref = quoteMarkRef('> Kiro Crew')
    ref(row)
    expect(registeredRanges('mc-quote-mark').map(r => r.toString())).toEqual(['Kiro Crew'])
    expect(registeredRanges('mc-quote-mark')[0].startContainer.textContent).toBe('Install Kiro Crew first')
    ref(null)
    expect(registeredRanges('mc-quote-mark')).toEqual([])
  })
})
