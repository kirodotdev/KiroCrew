import { describe, it, expect } from 'vitest'
import { QUOTE_TEXT_MAX, prependQuote, quoteBlock, quoteExcerpt, quoteFromMessage, readMessageQuote, stripQuoteBlock } from '../chat-core/composer/messageQuote'

describe('quoteFromMessage', () => {
  it('trims and keeps ts / mid only when given', () => {
    expect(quoteFromMessage('assistant', '  hello  ', 't1', 'm1')).toEqual({ role: 'assistant', text: 'hello', ts: 't1', mid: 'm1' })
    expect(quoteFromMessage('user', 'hi')).toEqual({ role: 'user', text: 'hi' })
  })
  it('refuses a row with nothing to quote', () => {
    expect(quoteFromMessage('user', '   \n ')).toBeNull()
  })
  it('carries the surface speaker name when given, trimmed, never blank', () => {
    expect(quoteFromMessage('assistant', 'a', 't', 'm', ' Worker ')?.author).toBe('Worker')
    expect(quoteFromMessage('assistant', 'a', 't', 'm', '  ')?.author).toBeUndefined()
  })
  it('never cuts through an emoji: the cap lands on a code-point boundary', () => {
    // Unbroken ASCII up to one unit short of the cap, then an astral character
    // straddling the cap, so a naive UTF-16 slice would keep its lead surrogate.
    const q = quoteFromMessage('assistant', 'a'.repeat(QUOTE_TEXT_MAX - 1) + '😀 tail')!
    expect(q.text).toBe('a'.repeat(QUOTE_TEXT_MAX - 1) + '…')
    expect(/[\ud800-\udbff](?![\udc00-\udfff])/.test(q.text)).toBe(false)
  })

  it('caps long text at a word and marks the cut', () => {
    const long = Array.from({ length: 400 }, (_, i) => `word${i}`).join(' ')
    const q = quoteFromMessage('assistant', long)!
    expect(q.text.length).toBeLessThanOrEqual(QUOTE_TEXT_MAX + 1)
    expect(q.text.endsWith('…')).toBe(true)
    expect(q.text.slice(0, -1).endsWith(' ')).toBe(false)
    expect(long.startsWith(q.text.slice(0, -1))).toBe(true)
  })
})

describe('quoteBlock / prependQuote / stripQuoteBlock', () => {
  const q = { role: 'assistant' as const, text: 'line one\nline two', ts: 't1' }

  it('serializes every line as a blockquote with an attribution line', () => {
    expect(quoteBlock(q)).toBe('> line one\n> line two\n> — quoting an earlier message from the assistant')
    expect(quoteBlock({ role: 'user', text: 'x' })).toBe('> x\n> — quoting an earlier message from the user')
  })

  it('opens the send with the block and a blank line before the typed text', () => {
    expect(prependQuote('  why?  ', q)).toBe(quoteBlock(q) + '\n\nwhy?')
  })

  it('sends the block alone when nothing was typed', () => {
    expect(prependQuote('', q)).toBe(quoteBlock(q))
  })

  it('strips exactly the block it wrote, and nothing else', () => {
    expect(stripQuoteBlock(prependQuote('why?', q), q)).toBe('why?')
    expect(stripQuoteBlock(prependQuote('', q), q)).toBe('')
  })

  it('leaves content alone when the block is not at the start (a hand-edited or foreign row)', () => {
    const edited = 'intro\n' + quoteBlock(q) + '\n\nwhy?'
    expect(stripQuoteBlock(edited, q)).toBe(edited)
    expect(stripQuoteBlock('> something else\n\nwhy?', q)).toBe('> something else\n\nwhy?')
  })
})

describe('quoteExcerpt', () => {
  it('drops markdown marks and joins lines with a middle dot', () => {
    expect(quoteExcerpt('Three things:\n\n- Staged files show as **chips**, not tokens.\n- Cancel restores the text *and* re-stages.\n## Done\n`code` and [a link](https://x.y)'))
      .toBe('Three things: · Staged files show as chips, not tokens. · Cancel restores the text and re-stages. · Done · code and a link')
  })
  it('drops fence lines and blockquote markers', () => {
    expect(quoteExcerpt('```ts\nconst a = 1\n```\n> quoted')).toBe('const a = 1 · quoted')
  })
  it('leaves a snake_case word alone', () => {
    expect(quoteExcerpt('use meta_quote here')).toBe('use meta_quote here')
  })
})

describe('readMessageQuote', () => {
  it('reads a well-formed record', () => {
    expect(readMessageQuote({ quote: { role: 'user', text: 'hi', ts: 't', mid: 'm' } })).toEqual({ role: 'user', text: 'hi', ts: 't', mid: 'm' })
    expect(readMessageQuote({ quote: { role: 'assistant', text: 'hi', author: 'Worker' } })).toEqual({ role: 'assistant', text: 'hi', author: 'Worker' })
  })
  it('drops optional fields that are not strings', () => {
    expect(readMessageQuote({ quote: { role: 'user', text: 'hi', ts: 5, mid: null } })).toEqual({ role: 'user', text: 'hi' })
  })
  it('caps an oversized text on read, so a foreign row cannot hand the card unbounded text', () => {
    const q = readMessageQuote({ quote: { role: 'user', text: 'w '.repeat(QUOTE_TEXT_MAX) } })!
    expect(q.text.length).toBeLessThanOrEqual(QUOTE_TEXT_MAX + 1)
    expect(q.text.endsWith('…')).toBe(true)
  })
  it('refuses anything a card could not draw', () => {
    expect(readMessageQuote(undefined)).toBeNull()
    expect(readMessageQuote({})).toBeNull()
    expect(readMessageQuote({ quote: 'text' })).toBeNull()
    expect(readMessageQuote({ quote: { role: 'system', text: 'hi' } })).toBeNull()
    expect(readMessageQuote({ quote: { role: 'user', text: '   ' } })).toBeNull()
    expect(readMessageQuote({ quote: { role: 'user' } })).toBeNull()
  })
})
