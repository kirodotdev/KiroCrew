import { describe, it, expect, onTestFinished, vi } from 'vitest'
import { prClosed, prFactTags } from './prFactTags'

const facts = (reads: Record<string, unknown>) => ({ label: 'a/b#1', url: 'u', reads }) as Parameters<typeof prFactTags>[0]

describe('prFactTags', () => {
  it("draws tags only from the welcome's own read", () => {
    expect(prFactTags(facts({ probe: { facts: { state: 'merged' }, observed_at: 1 } }))).toEqual([])
    const warm = facts({ warm_brief: { facts: { state: 'closed' }, observed_at: 1 } })
    expect(prFactTags(warm).map(t => t.text)).toContain('closed')
  })

  it("decides closed from the welcome's own read only", () => {
    expect(prClosed(facts({ warm_brief: { facts: { state: 'merged' }, observed_at: 10 } }))).toBe(true)
    expect(prClosed(facts({ warm_brief: { facts: { state: 'open' }, observed_at: 10 } }))).toBe(false)
    expect(prClosed(facts({}))).toBe(false)
  })

  it('dates a read from another day, so it never reads as today', () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    onTestFinished(() => { vi.useRealTimers() })
    vi.setSystemTime(new Date(2026, 10, 9, 15, 0))
    const at = (day: number) => new Date(2026, 10, day, 14, 5).getTime() / 1000
    expect(prFactTags(facts({ warm_brief: { facts: { state: 'open' }, observed_at: at(6) } })).map(t => t.text)).toContain('read 2026-11-06 14:05')
    expect(prFactTags(facts({ warm_brief: { facts: { state: 'open' }, observed_at: at(9) } })).map(t => t.text)).toContain('read 14:05')
  })
})
