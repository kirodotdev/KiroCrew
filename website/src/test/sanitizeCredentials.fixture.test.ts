import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import { sanitizeCredentials, scanKeyedValue } from '../utils/sanitize'

// The keyed-value fixture is generated from the backend's canonical value scanner
// (`test/redaction_keyed_value_fixture.py`, committed as
// `test/fixtures/redaction_keyed_values.json`) and pinned on the backend side by
// `test/test_redaction_keyed_value_fixture.py`. This mirror ports that scanner, and
// a port that drifts on a quote or escape shape is a leak one surface has and the
// other does not -- which is how the mirror once showed the second fragment of a
// concatenated secret behind a doubled quote. Every row here is the backend's
// answer with its tag spelled `[REDACTED]`; backend tags already in the INPUT are
// left as they are, so the mirror is a fixed point over the backend's own output.
const WEBSITE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const FIXTURE = path.resolve(WEBSITE_ROOT, '../test/fixtures/redaction_keyed_values.json')

interface Row {
  key: string
  shape: string
  text: string
  expected: string
  expected_mirror: string
  live: boolean
}

// The file is the generator's compact encoding (`encode_rows`): the keys once, then
// one entry per shape with the key written as the slot `{key}`, or the rows
// themselves where a shape's rows differ by more than the key; field names are one
// letter each. Decoded here exactly as the backend suite decodes it (`decode_rows`).
interface ShortRow {
  k: string
  s: string
  t: string
  e: string
  w: number
  m: string
  l: boolean
}
type ShapeEntry = { s: string; t: string; e: string; m: string; w: number; l: boolean } | { s: string; rows: ShortRow[] }
interface Fixture {
  keys: string[]
  shapes: ShapeEntry[]
}

const KEY_SLOT = '{key}'

function decodeRows(doc: Fixture): Row[] {
  const out: Row[] = []
  doc.keys.forEach((key, index) => {
    for (const entry of doc.shapes) {
      if ('rows' in entry) {
        const r = entry.rows[index]
        out.push({ key: r.k, shape: r.s, text: r.t, expected: r.e, expected_mirror: r.m, live: r.l })
        continue
      }
      const fill = (s: string) => s.split(KEY_SLOT).join(key)
      out.push({
        key,
        shape: entry.s,
        text: fill(entry.t),
        expected: fill(entry.e),
        expected_mirror: fill(entry.m),
        live: entry.l,
      })
    }
  })
  return out
}

const rows: Row[] = decodeRows(JSON.parse(readFileSync(FIXTURE, 'utf8')) as Fixture)

describe('sanitizeCredentials: the shared keyed-value fixture', () => {
  it('loads the generated fixture', () => {
    expect(rows.length).toBeGreaterThan(150)
  })

  for (const row of rows) {
    it(`${row.key} ${row.shape}`, () => {
      const once = sanitizeCredentials(row.text)
      expect(once).toBe(row.expected_mirror)
      // A second pass over the mirror's own output changes nothing.
      expect(sanitizeCredentials(once)).toBe(once)
    })
  }

  it('reads an empty value as no value, and a lone opener as pending-shaped', () => {
    expect(scanKeyedValue('k=', 2)).toEqual({ start: 2, end: 2, closes: true, opener: '' })
    expect(scanKeyedValue('k=""', 2)).toEqual({ start: 3, end: 3, closes: true, opener: '"' })
    expect(scanKeyedValue('k="', 2)).toEqual({ start: 3, end: 3, closes: false, opener: '"' })
  })
})
