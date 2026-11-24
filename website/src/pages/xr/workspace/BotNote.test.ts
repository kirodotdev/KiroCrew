import { describe, expect, it } from 'vitest'
import { noteRows } from './BotNote'

const wide = (s: string) => s.length * 10

describe('a plate\'s line (D88 1)', () => {
  it('is one row when it fits', () => {
    expect(noteRows('Otto: its sheet is in your hand', wide, 400)).toEqual(['Otto: its sheet is in your hand'])
  })
  it('breaks into two rows at the space nearest its middle, and loses no word', () => {
    const text = 'Sage: this Bot\'s page cannot be opened here.'
    const rows = noteRows(text, wide, 300)
    expect(rows).toHaveLength(2)
    expect(rows.join(' ')).toBe(text)
    expect(Math.abs(rows[0].length - rows[1].length)).toBeLessThan(8)
  })
  it('stays one row when it has no space to break at', () => {
    expect(noteRows('x'.repeat(80), wide, 300)).toHaveLength(1)
  })
})
