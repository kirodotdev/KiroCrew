/** A bare `\r`, U+2028 or U+2029 ends an `[OPTIONS:]` line, as `\n` does; channels mirror these. */
import { describe, it, expect } from 'vitest'
import { parseOptions } from '../app-sdk/protocol'

const BREAKS = ['\r', '\u2028', '\u2029']

describe('every line break ends a marker line', () => {
  it.each([
    ['Body\n**[OPTIONS: A | B]**', 'Body', ['A', 'B']],
    ['[OPTIONS: dropped\n[OPTIONS: A | B]', '[OPTIONS: dropped', ['A', 'B']],
    ['Body\n[OPTIONS: A] [OPTIONS: B]', 'Body', ['B']],
    ['[OPTIONS: A] [OPTIONS: B then arr[0]\n[OPTIONS: C]', '[OPTIONS: A] [OPTIONS: B then arr[0]', ['C']],
    ['[OPTIONS: A | B]\nThen restart.', 'Then restart.', ['A', 'B']],
  ])('%j reads alike with every break', (raw, text, options) => {
    expect(parseOptions(raw)).toMatchObject({ text, options })
    for (const br of BREAKS) {
      const got = parseOptions(raw.replaceAll('\n', br))
      expect(got, JSON.stringify(br)).toMatchObject({ text: text.replaceAll('\n', br), options })
    }
  })

  it.each(BREAKS.map(br => [JSON.stringify(br), br]))(
    'a marker whose labels cross a %s break stays text',
    (_name, br) => {
      const raw = `[OPTIONS: A${br}B | C]`
      expect(parseOptions(raw)).toMatchObject({ text: raw, options: [] })
    },
  )

  it('a CRLF reply parses its marker line', () => {
    expect(parseOptions('Body\r\n[OPTIONS: A | B]\r\n')).toMatchObject({ text: 'Body', options: ['A', 'B'] })
  })
})
