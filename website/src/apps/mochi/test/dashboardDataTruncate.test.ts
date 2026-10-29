import { describe, expect, it } from 'vitest'

import { formatActivity } from '../dashboardData'

const entry = (content: string) => ({ type: 'plan', content, ts: '2026-01-01T00:00:00Z' })

describe('formatActivity with astral characters', () => {
  it('drops an emoji the 150-unit cap would cut in half instead of keeping its high surrogate', () => {
    // 149 ASCII units, then an emoji occupying UTF-16 units 149-150.
    const [row] = formatActivity([entry(`${'a'.repeat(149)}😀 and the rest`)])
    expect(row.content).toBe('a'.repeat(149))
  })

  it('keeps an emoji that ends exactly at the cap', () => {
    const [row] = formatActivity([entry(`${'a'.repeat(148)}😀 and the rest`)])
    expect(row.content).toBe(`${'a'.repeat(148)}😀`)
  })

  it('leaves long ASCII at 150 units', () => {
    const [row] = formatActivity([entry('x'.repeat(200))])
    expect(row.content).toBe('x'.repeat(150))
  })
})
