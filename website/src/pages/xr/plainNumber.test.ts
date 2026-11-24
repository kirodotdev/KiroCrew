import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { PLAIN_SPACES, plainNumber } from './glance'

const file = JSON.parse(readFileSync(resolve(process.cwd(), '../test/fixtures/xr_layout_cases.json'), 'utf-8')) as { plain_number_cases: [string, number | null][]; plain_number_spaces: number[] }

/** The rule as it stood before M182: this page's own `\s`, in one pattern that tried each split of a run of spaces. */
function theRuleBefore(text: string): number | null {
  const m = /^\s*-?\d+(?:[.,]\d+)*\s*%?\s*$/.exec(text)
  if (!m) return null
  const n = Number(text.replace(/[%\s]/g, '').replace(/(\d),(\d{3})/g, '$1$2').replace(',', '.'))
  return Number.isFinite(n) ? n : null
}

describe('a reading that is a plain number: one rule for the tab and the gateway (M175, M176, M182)', () => {
  it('has cases', () => { expect(file.plain_number_cases.length).toBeGreaterThan(50) })

  for (const [text, number] of file.plain_number_cases) {
    it(`reads ${JSON.stringify(text)} as ${number}`, () => { expect(plainNumber(text)).toBe(number) })
  }

  it('counts as a space the one written-out list that the gateway holds too, which is what `\\s` took here', () => {
    expect([...PLAIN_SPACES]).toEqual(file.plain_number_spaces)
    // Every sign of two bytes: it is in the list exactly when this language's `\s` takes it.
    for (let c = 0; c <= 0xffff; c += 1) expect([c, PLAIN_SPACES.includes(c)]).toEqual([c, /\s/.test(String.fromCharCode(c))])
  })

  it('draws as a number exactly what was drawn as one before: no text changes in the room', () => {
    const signs = [...Array.from({ length: 0x100 }, (_, c) => c), 0x1680, ...Array.from({ length: 20 }, (_, i) => 0x1ffe + i), 0x2028, 0x2029, 0x202f, 0x205f, 0x2060, 0x3000, 0x3001, 0xfeff, 0xff11, 0x0661].map(c => String.fromCharCode(c))
    const shapes = ['{c}12', '12{c}', '1{c}2', '12{c}%', '12%{c}', '{c}-1,234.5 %', '{c}', '{c}{c}', '1,234{c},567']
    const differ: string[] = []
    for (const c of signs) for (const shape of shapes) { const text = shape.replaceAll('{c}', c); if (!Object.is(plainNumber(text), theRuleBefore(text))) differ.push(JSON.stringify(text)) }
    expect(differ).toEqual([])
  })

  it('takes out all the commas of a number with thousands, and reads every other text as before', () => {
    // 9,360 texts: one to four groups of one to four digits, joined by commas and points. The gateway's unit holds the same family.
    const pieces = ['7', '45', '123', '0123']
    const family: string[] = []
    const grow = (core: string, left: number) => {
      for (const sign of ['', '-']) for (const tail of ['', ' %']) family.push(sign + core + tail)
      if (left > 0) for (const join of [',', '.']) for (const g of pieces) grow(core + join + g, left - 1)
    }
    for (const g of pieces) grow(g, 3)
    expect(family.length).toBe(9360)
    // Written apart from the reader's pattern: every comma stands before exactly three digits.
    const hasThousands = (text: string): boolean => {
      const core = text.replace(/ %$/, '').replace(/^-/, '')
      const dot = core.indexOf('.')
      const whole = dot < 0 ? core : core.slice(0, dot)
      const rest = dot < 0 ? '' : core.slice(dot + 1)
      const [first, ...groups] = whole.split(',')
      return groups.length > 0 && first.length >= 1 && first.length <= 3 && groups.every(g => g.length === 3) && (dot < 0 || /^\d+$/.test(rest))
    }
    const changed = family.filter(t => !Object.is(plainNumber(t), theRuleBefore(t)))
    expect(changed.length).toBeGreaterThan(0)
    for (const t of changed) expect([t, hasThousands(t) && t.split(',').length >= 3]).toEqual([t, true])
    for (const t of family) if (hasThousands(t)) expect([t, plainNumber(t)]).toEqual([t, Number(t.replace(/ %$/, '').replace(/,/g, ''))])
    expect(changed.length).toBe(family.filter(t => hasThousands(t) && t.split(',').length >= 3).length)
    expect([plainNumber('1,234'), theRuleBefore('1,234')]).toEqual([1234, 1234])
  })

  it('reads a text of 32 kB that is almost a number at once: six reads in under 100 ms, where the old rule took seconds', () => {
    // The least of three readings: another worker's slice cannot hold all three back.
    const least = (text: string): number => Math.min(...[0, 1, 2].map(() => { const t = performance.now(); for (let i = 0; i < 6; i += 1) expect(plainNumber(text)).toBeNull(); return performance.now() - t }))
    const n = 32768
    for (const text of ['1' + ' '.repeat(n) + 'x', ' '.repeat(n), '1 '.repeat(n / 2), '1'.repeat(n) + 'x', '1' + '\t '.repeat(n / 4) + '%' + ' '.repeat(n / 2) + 'x', '1' + ',1'.repeat(n / 2) + 'x']) expect([text.slice(0, 8), least(text) < 100]).toEqual([text.slice(0, 8), true])
    expect(plainNumber(' '.repeat(n) + '12' + ' '.repeat(n) + '%' + ' '.repeat(n))).toBe(12)
  })
})
