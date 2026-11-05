/**
 * Two `[OPTIONS:]` markers on one line, and a row whose marker renders nothing.
 */
import { describe, it, expect } from 'vitest'
import type { ChatMessage } from '../types'
import { parseOptions } from '../app-sdk/protocol'
import { searchableText } from '../utils/searchableText'

describe('two markers on ONE line', () => {
  it('same-KIND pair: both are stripped and the LAST supplies the options', () => {
    // Previously the earlier of two markers could not match at all, so its raw text
    // leaked. Both are recognised now, and "the last marker wins" decides the options.
    const { options, text } = parseOptions('Body [OPTIONS: A] [OPTIONS: B]')
    expect(options).toEqual(['B'])
    expect(text).toBe('Body')
  })

  it('still refuses a marker followed by ordinary prose on the same line', () => {
    const { options, text } = parseOptions('See [OPTIONS: A | B] for details')
    expect(options).toEqual([])
    expect(text).toEqual('See [OPTIONS: A | B] for details')
  })

  it.each([
    ['two markers', '[OPTIONS: A] [OPTIONS: B] in docs'],
    ['three markers', '[OPTIONS: A] [OPTIONS: B] [OPTIONS: C] see above'],
  ])('a sibling CHAIN that does not reach the line end parses NOTHING — %s', (_name, raw) => {
    // All-or-nothing: admitting the first marker while the last declined deleted the first
    // marker's text from the message and returned it as a pill nobody wrote.
    const { options, text } = parseOptions(raw)
    expect(options, raw).toEqual([])
    expect(text, raw).toEqual(raw)
  })
})

describe('a row the strip would empty keeps a marker only when nothing renders it', () => {
  const msg = (content: string): ChatMessage =>
    ({ role: 'assistant', content }) as unknown as ChatMessage

  it('still empties a row whose only marker offers pills', () => {
    expect(parseOptions('[OPTIONS: Alpha | Beta]').text).toBe('')
    expect(searchableText(msg('[OPTIONS: Alpha | Beta]'))).not.toContain('Alpha')
  })

  it('keeps the row, on screen and in search, when the marker that WOULD render offers nothing', () => {
    // The pills come from the LAST marker, so an earlier one's options must not vouch for a
    // final one that renders none: that left a row with neither prose nor pills.
    const raw = '[OPTIONS: A] [OPTIONS:]'
    expect(parseOptions(raw).options).toEqual([])
    expect(parseOptions(raw).text).toBe(raw)
    expect(searchableText(msg(raw))).toBe(raw)
  })
})
