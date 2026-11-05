/**
 * A marker inside a still-open head is not a marker, and a head closed before inline prose
 * does not stay open.
 */
import { describe, it, expect } from 'vitest'

import { findOptionMarkers } from '../app-sdk/protocol/optionMarker'
import { parseOptions } from '../app-sdk/protocol/options'

describe('a marker nested inside an unclosed head', () => {
  it('refuses the nested marker', () => {
    expect(findOptionMarkers('[OPTIONS: dropped [OPTIONS: A | B]')).toEqual([])
  })
})

describe('a head closed before inline prose does not stay open', () => {
  // A closer followed by `,` continues the label list only when another closer follows
  // before the next opener on that line. Otherwise the head ended there.
  it.each([
    ['comma then prose', 'Use [OPTIONS: A | B], then pick [OPTIONS: Yes | No]', ['Yes', 'No'], 'Use [OPTIONS: A | B], then pick'],
    ['comma, no space', 'Use [OPTIONS: A],then [OPTIONS: Yes | No]', ['Yes', 'No'], 'Use [OPTIONS: A],then'],
  ])('keeps the line-final marker — %s', (_name, raw, options, text) => {
    expect(parseOptions(raw).options).toEqual(options)
    expect(parseOptions(raw).text).toBe(text)
  })

  it('CONTROL: a label that carries a closer and continues to one still parses', () => {
    expect(parseOptions('[OPTIONS: Alpha ] | Bravo ]]').options).toEqual(['Alpha ]', 'Bravo ]'])
  })

  it('CONTROL: a comma-separated label that carries a closer still parses', () => {
    expect(parseOptions('[OPTIONS: Alpha ], Bravo ]').options).toEqual(['Alpha ]', 'Bravo'])
  })

  it('CONTROL: a head that never closes before the next head still refuses it', () => {
    expect(findOptionMarkers('[OPTIONS: x, then [OPTIONS: Yes | No]')).toEqual([])
  })
})
