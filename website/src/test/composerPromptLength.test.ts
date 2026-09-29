import { describe, it, expect } from 'vitest'

import {
  PROMPT_LENGTH_WARN_RATIO,
  checkPromptLength,
  measurePrompt,
  sentPromptText,
} from '../components/composerPromptLength'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'

describe('measurePrompt', () => {
  it('counts an empty prompt as zero', () => {
    expect(measurePrompt('')).toBe(0)
  })

  it('estimates a quarter token per ASCII character, rounded up', () => {
    expect(measurePrompt('abcd')).toBe(1)
    expect(measurePrompt('abcde')).toBe(2)
  })

  it('counts one token per non-ASCII code point', () => {
    expect(measurePrompt('é')).toBe(1)
    expect(measurePrompt('中文')).toBe(2)
    expect(measurePrompt('😀')).toBe(1)
  })
})

describe('sentPromptText', () => {
  it('expands collapsed paste chips back to the pasted content', () => {
    const block: PasteBlock = { id: 'p1', seq: 1, lines: 3, content: 'line a\nline b\nline c' }
    const value = `see ${formatToken(block)} please`
    expect(sentPromptText(value, [block])).toBe('see line a\nline b\nline c please')
  })

  it('trims outer whitespace from the value and expanded content', () => {
    const block: PasteBlock = { id: 'p1', seq: 1, lines: 1, content: '  pasted content  ' }
    expect(sentPromptText(`  ${formatToken(block)}  `, [block])).toBe('pasted content')
    expect(sentPromptText('  plain  ', [])).toBe('plain')
  })
})

describe('checkPromptLength', () => {
  const window = 1000

  it('is ok under the warn threshold', () => {
    const c = checkPromptLength(899, window)
    expect(c.level).toBe('ok')
    expect(c.overBy).toBe(0)
  })

  it('is near at the warn threshold and at exactly the limit', () => {
    expect(checkPromptLength(window * PROMPT_LENGTH_WARN_RATIO, window).level).toBe('near')
    const atLimit = checkPromptLength(window, window)
    expect(atLimit.level).toBe('near')
    expect(atLimit.overBy).toBe(0)
  })

  it('is over past the limit and reports by how much', () => {
    const c = checkPromptLength(1250, window)
    expect(c).toMatchObject({ level: 'over', limit: window, used: 1250, overBy: 250 })
  })

  it('is ok for huge text when the context window is unknown or invalid', () => {
    for (const unknown of [0, undefined, Number.NaN, -5]) {
      expect(checkPromptLength(10_000_000, unknown).level).toBe('ok')
    }
  })

  it('does not count outer whitespace stripped by the send path', () => {
    const text = `${' '.repeat(1000)}${'a'.repeat(400)}${' '.repeat(1000)}`
    const c = checkPromptLength(measurePrompt(sentPromptText(text, [])), 100)
    expect(c).toMatchObject({ level: 'near', used: 100 })
  })
})
