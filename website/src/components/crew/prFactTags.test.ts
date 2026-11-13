import { describe, it, expect, onTestFinished, vi } from 'vitest'
import { prFactTags } from './prFactTags'

const facts = (reads: Record<string, unknown>) => ({ label: 'a/b#1', url: 'u', reads }) as Parameters<typeof prFactTags>[0]

describe('prFactTags', () => {
  it("draws tags only from the welcome's own read, bad facts first", () => {
    expect(prFactTags(facts({ probe: { facts: { state: 'merged' }, observed_at: 1 } }))).toEqual([])
    const warm = facts({ warm_brief: { facts: { state: 'CLOSED', mergeable: 'CONFLICTING' }, observed_at: 1 } })
    const tags = prFactTags(warm)
    expect(tags.map(t => t.text).slice(0, 2)).toEqual(['closed', 'has conflicts'])
    expect(tags.every((t, i) => !t.bad || tags.slice(0, i).every(p => p.bad))).toBe(true)
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
