import { describe, expect, it } from 'vitest'
import {
  INLINE_BOLD,
  INLINE_CODE,
  INLINE_ITALIC,
  INLINE_STRIKETHROUGH,
  MAX_STYLED_RUN,
  isPlainInline,
  parseInlineMarkdown,
  type InlineSegment,
} from './composerInlineMarkdown'

/** Compact notation: each segment as [text, flags], flags drawn from
 *  b(old) i(talic) s(trike) c(ode) m(arker). */
function shape(segments: InlineSegment[]): Array<[string, string]> {
  return segments.map(segment => {
    let flags = ''
    if (segment.format & INLINE_BOLD) flags += 'b'
    if (segment.format & INLINE_ITALIC) flags += 'i'
    if (segment.format & INLINE_STRIKETHROUGH) flags += 's'
    if (segment.format & INLINE_CODE) flags += 'c'
    if (segment.marker) flags += 'm'
    return [segment.text, flags]
  })
}

const parse = (text: string) => shape(parseInlineMarkdown(text))

describe('parseInlineMarkdown', () => {
  it('styles bold, italic, strikethrough and inline code with visible markers', () => {
    expect(parse('**important**')).toEqual([['**', 'm'], ['important', 'b'], ['**', 'm']])
    expect(parse('__important__')).toEqual([['__', 'm'], ['important', 'b'], ['__', 'm']])
    expect(parse('*word*')).toEqual([['*', 'm'], ['word', 'i'], ['*', 'm']])
    expect(parse('_word_')).toEqual([['_', 'm'], ['word', 'i'], ['_', 'm']])
    expect(parse('~~word~~')).toEqual([['~~', 'm'], ['word', 's'], ['~~', 'm']])
    expect(parse('`code`')).toEqual([['`', 'm'], ['code', 'c'], ['`', 'm']])
  })

  it('styles spans inside surrounding plain text', () => {
    expect(parse('say **hi** now')).toEqual([
      ['say ', ''], ['**', 'm'], ['hi', 'b'], ['**', 'm'], [' now', ''],
    ])
  })

  it('nests styles, including ***both***', () => {
    expect(parse('***both***')).toEqual([
      ['*', 'm'], ['**', 'im'], ['both', 'bi'], ['**', 'im'], ['*', 'm'],
    ])
    expect(parse('**a *b* c**')).toEqual([
      ['**', 'm'], ['a ', 'b'], ['*', 'bm'], ['b', 'bi'], ['*', 'bm'], [' c', 'b'], ['**', 'm'],
    ])
    expect(parse('~~**x**~~')).toEqual([
      ['~~', 'm'], ['**', 'sm'], ['x', 'bs'], ['**', 'sm'], ['~~', 'm'],
    ])
  })

  it('leaves unmatched or whitespace-flanked markers as plain text', () => {
    for (const text of ['**', '**open', 'a * b', 'a ** b **', '** not bold**', '*', 'x*', '~~', '`', 'a `b']) {
      expect(parse(text)).toEqual([[text, '']])
    }
  })

  it('does not treat intraword underscores as emphasis', () => {
    expect(parse('snake_case_name')).toEqual([['snake_case_name', '']])
    expect(parse('@src/my_file_name.ts')).toEqual([['@src/my_file_name.ts', '']])
  })

  it('keeps escaped markers literal', () => {
    expect(parse('\\*not italic\\*')).toEqual([['\\*not italic\\*', '']])
    expect(parse('\\**x**')).toEqual([['\\*', ''], ['*', 'm'], ['x', 'i'], ['*', 'm'], ['*', '']])
    expect(parse('\\`not code`')).toEqual([['\\`not code`', '']])
  })

  it('does not style markers inside a code span', () => {
    expect(parse('`a **b** c`')).toEqual([['`', 'm'], ['a **b** c', 'c'], ['`', 'm']])
    expect(parse('*a `b* c`')).toEqual([['*a ', ''], ['`', 'm'], ['b* c', 'c'], ['`', 'm']])
    // A backslash inside code is literal and does not escape the closer.
    expect(parse('`a\\`')).toEqual([['`', 'm'], ['a\\', 'c'], ['`', 'm']])
  })

  it('leaves multi-backtick spans literal', () => {
    expect(parse('``x``')).toEqual([['``x``', '']])
    expect(parse('```js')).toEqual([['```js', '']])
    // A single-backtick span may contain a longer run.
    expect(parse('`a``b`')).toEqual([['`', 'm'], ['a``b', 'c'], ['`', 'm']])
  })

  it('uses only double tildes for strikethrough', () => {
    expect(parse('~one~')).toEqual([['~one~', '']])
    expect(parse('~~~three~~~')).toEqual([['~~~three~~~', '']])
  })

  it('returns an empty list for empty input and plain text as one segment', () => {
    expect(parseInlineMarkdown('')).toEqual([])
    expect(parse('hello world')).toEqual([['hello world', '']])
    expect(isPlainInline(parseInlineMarkdown('hello world'))).toBe(true)
    expect(isPlainInline(parseInlineMarkdown('**x**'))).toBe(false)
  })

  it('returns an over-long run as one plain segment', () => {
    const text = `**${'a'.repeat(MAX_STYLED_RUN)}**`
    expect(parseInlineMarkdown(text)).toEqual([{ text, format: 0, marker: false }])
  })

  it('returns a run with too many delimiters as plain', () => {
    const text = '*a '.repeat(1_500)
    expect(isPlainInline(parseInlineMarkdown(text))).toBe(true)
  })

  it('round-trips arbitrary input losslessly', () => {
    const alphabet = ['*', '*', '_', '~', '`', '\\', ' ', 'a', 'b', '1', '.', 'é', '😀']
    let seed = 7
    const random = () => {
      seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648
      return seed / 2_147_483_648
    }
    for (let sample = 0; sample < 3_000; sample += 1) {
      const length = Math.floor(random() * 24)
      let text = ''
      for (let i = 0; i < length; i += 1) text += alphabet[Math.floor(random() * alphabet.length)]
      const segments = parseInlineMarkdown(text)
      expect(segments.map(segment => segment.text).join('')).toBe(text)
      for (const [index, segment] of segments.entries()) {
        expect(segment.text.length).toBeGreaterThan(0)
        const previous = segments[index - 1]
        if (previous) {
          expect(previous.format !== segment.format || previous.marker !== segment.marker).toBe(true)
        }
      }
    }
  })
})
