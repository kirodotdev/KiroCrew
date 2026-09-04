import { describe, expect, it } from 'vitest'
import {
  prependQuoteRefs,
  serializeQuoteRefs,
  type QuoteRef,
} from '../utils/quoteRefs'

const quote = (overrides: Partial<QuoteRef> = {}): QuoteRef => ({
  key: 'quote-1',
  role: 'Assistant',
  time: '10:22',
  text: 'selected text',
  ...overrides,
})

describe('quote reference serialization', () => {
  it('serializes multiline prose as one attributed blockquote', () => {
    expect(serializeQuoteRefs([
      quote({ text: 'first line\nsecond line\nthird line' }),
    ])).toBe(
      '> [Assistant · 10:22] first line\n' +
      '> second line\n' +
      '> third line',
    )
  })

  it('preserves code indentation inside a blockquoted fence', () => {
    expect(serializeQuoteRefs([
      quote({ text: 'if (ready) {\n  run()\n}', code: true }),
    ])).toBe(
      '> [Assistant · 10:22]\n' +
      '> ```\n' +
      '> if (ready) {\n' +
      '>   run()\n' +
      '> }\n' +
      '> ```',
    )
  })

  it('emits nothing for an empty quote collection', () => {
    expect(serializeQuoteRefs([])).toBe('')
  })

  it('prepends quotes before typed instructions with one blank separator', () => {
    expect(prependQuoteRefs('Explain why this matters.', [quote()])).toBe(
      '> [Assistant · 10:22] selected text\n\nExplain why this matters.',
    )
  })

  it('preserves typed text exactly when there are no staged quotes', () => {
    expect(prependQuoteRefs('  keep my spacing  ', [])).toBe('  keep my spacing  ')
  })

  it('returns the quote block by itself when the composer has no prose', () => {
    expect(prependQuoteRefs('', [quote()])).toBe(
      '> [Assistant · 10:22] selected text',
    )
  })
})
