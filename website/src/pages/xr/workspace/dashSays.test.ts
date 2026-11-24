import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { DASH, DASH_KINDS, DASH_SAYS, DASH_UNITS, LAMP_STATES, isPart, partSays, partValue, readLayout, sayNumber, type DashPart } from './dash3d'

type Fields = Record<string, unknown>
const file = JSON.parse(readFileSync(resolve(process.cwd(), '../test/fixtures/xr_layout_cases.json'), 'utf-8')) as { value_cases: { name: string; layout: unknown; fields: Fields; says: string[] }[] }
const parts = (layout: unknown): DashPart[] => readLayout(layout).parts.filter(isPart)

describe('what a part shows, in the fixed words of the write\'s reply (D169): one file for the tab and the gateway', () => {
  it('has cases, and each names every part of a whole layout', () => {
    expect(file.value_cases.length).toBeGreaterThan(10)
    for (const c of file.value_cases) expect(parts(c.layout)).toHaveLength(c.says.length)
  })

  for (const c of file.value_cases) {
    it(c.name, () => {
      expect(parts(c.layout).map(p => partSays(p, c.fields))).toEqual(c.says)
    })
  }

  it('says a dash exactly where the room draws one', () => {
    for (const c of file.value_cases) {
      for (const p of parts(c.layout)) expect([c.name, p.field, partSays(p, c.fields).includes('a dash: ')]).toEqual([c.name, p.field, !partValue(p, c.fields).found])
    }
  })

  it('is made of the kind, a unit, numbers and the fixed words: never a name, a field\'s name or a text a field holds', () => {
    const words = new Set([...DASH_KINDS, ...LAMP_STATES, ...[...Object.values(DASH_SAYS), ...DASH_UNITS].flatMap(s => s.match(/[A-Za-z]+/g) ?? []), 'of'])
    for (const c of file.value_cases) {
      for (const line of parts(c.layout).map(p => partSays(p, c.fields))) {
        const rest = line.replace(/-?\d+(\.\d+)?/g, ' ')
        expect([c.name, (rest.match(/[A-Za-z]+/g) ?? []).filter(w => !words.has(w))]).toEqual([c.name, []])
      }
    }
  })

  it('writes a number whole or with two places, a half away from 0, and only names one of more than 12 digits', () => {
    expect([0, -0, 7, 100, 1200, 12.5, 0.125, -0.125, 1.005, 2.999, -3.456, 0.001, 999999999999].map(sayNumber)).toEqual(['0', '0', '7', '100', '1200', '12.5', '0.13', '-0.13', '1', '3', '-3.46', '0', '999999999999'])
    expect([1e12, -1e12, 1e300].map(sayNumber)).toEqual([DASH_SAYS.big, DASH_SAYS.big, DASH_SAYS.big])
  })

  it('counts every lamp and names the states of the drawn ones only', () => {
    const [lamps] = parts({ v: DASH.version, parts: [{ kind: 'lamps', name: 'L', field: 'rows' }] })
    const rows = Array.from({ length: DASH.mostLamps + 8 }, (_, i) => (i < DASH.mostLamps ? 'idle' : 'failed'))
    expect(partSays(lamps, { rows })).toBe(`lamps ${DASH.mostLamps + 8}, ${DASH.mostLamps} drawn (${DASH.mostLamps} idle)`)
  })
})
