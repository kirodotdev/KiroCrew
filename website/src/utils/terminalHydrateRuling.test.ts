import { describe, it, expect, vi } from 'vitest'
import { createTerminalHydrateRuling, liveSessionIds } from './terminalHydrateRuling'

const listing = (...entries: [string, boolean][]) => ({
  enabled: true,
  sessions: entries.map(([session_id, alive]) => ({ session_id, alive })),
})

function setup(restored: string[], held = restored) {
  let ids = [...held]
  const drop = vi.fn((gone: ReadonlySet<string>) => { ids = ids.filter(id => !gone.has(id)) })
  const emit = vi.fn()
  const ruling = createTerminalHydrateRuling(new Set(restored), () => ids, drop, emit)
  return { ruling, drop, emit, held: () => ids }
}

describe('liveSessionIds', () => {
  it('reads the live ids from a full answer', () => {
    expect(liveSessionIds(listing(['a', true], ['b', false]))).toEqual(new Set(['a']))
  })

  it.each([
    ['null', null],
    ['disabled', { enabled: false, sessions: [] }],
    ['no list', { enabled: true }],
    ['malformed entry', { enabled: true, sessions: [{ session_id: 'a' }] }],
  ])('does not rule on a %s payload', (_name, payload) => {
    expect(liveSessionIds(payload)).toBeNull()
  })
})

describe('createTerminalHydrateRuling', () => {
  it('starts settled when nothing was restored', () => {
    expect(setup([]).ruling.isPending()).toBe(false)
  })

  it('settles at once when every restored session is live', () => {
    const { ruling, drop, emit } = setup(['a', 'b'])
    expect(ruling.isPending()).toBe(true)
    expect(ruling.reconcile(listing(['a', true], ['b', true]))).toEqual([])
    expect(ruling.isPending()).toBe(false)
    expect(emit).toHaveBeenCalledTimes(1)
    expect(ruling.confirm(listing())).toEqual([])
    expect(drop).not.toHaveBeenCalled()
  })

  it('drops only the suspects the second look still misses', () => {
    const { ruling, drop, held } = setup(['live', 'opening', 'gone'])
    expect(ruling.reconcile(listing(['live', true], ['gone', false]))).toEqual(['opening', 'gone'])
    expect(ruling.isPending()).toBe(true)
    expect(drop).not.toHaveBeenCalled()
    expect(ruling.confirm(listing(['live', true], ['opening', true]))).toEqual(['gone'])
    expect(held()).toEqual(['live', 'opening'])
    expect(ruling.isPending()).toBe(false)
  })

  it('keeps every tab when either look cannot rule', () => {
    const first = setup(['a'])
    expect(first.ruling.reconcile(null)).toEqual([])
    expect(first.ruling.isPending()).toBe(false)

    const second = setup(['a'])
    expect(second.ruling.reconcile(listing())).toEqual(['a'])
    expect(second.ruling.confirm(null)).toEqual([])
    expect(second.drop).not.toHaveBeenCalled()
    expect(second.ruling.isPending()).toBe(false)
  })

  it('never names a session that was not restored', () => {
    const { ruling } = setup(['old'], ['old', 'minted'])
    expect(ruling.reconcile(listing())).toEqual(['old'])
  })

  it('rules once: later looks are no-ops', () => {
    const { ruling, drop } = setup(['a'])
    ruling.reconcile(listing())
    expect(ruling.reconcile(listing(['a', true]))).toEqual([])
    expect(ruling.confirm(listing())).toEqual(['a'])
    expect(ruling.confirm(listing())).toEqual([])
    expect(drop).toHaveBeenCalledTimes(1)
  })
})
