import { describe, expect, it } from 'vitest'
import type { XrBot } from '../model'
import { cutNames, fitLine, foldNext, homeNames, placeNames, signs } from './homeNames'

const bot = (slug: string, asks: number[], state = 'idle') => ({ slug, label: slug, state, needs: asks.map(ts => ({ ts })) }) as unknown as XrBot

describe('homeNames', () => {
  it('names the askers first, the most asks first, then the failed', () => {
    const got = homeNames([bot('pico', [], 'failed'), bot('juno', [5]), bot('atlas', [9, 8]), bot('mira', [])])
    expect(got.names.map(n => [n.slug, n.asks, n.failed])).toEqual([['atlas', 2, false], ['juno', 1, false], ['pico', 0, true]])
    expect(got.more).toBe(0)
  })
  it('puts the older ask first among Bots with as many asks', () => {
    expect(homeNames([bot('b', [7]), bot('a', [3])]).names.map(n => n.slug)).toEqual(['a', 'b'])
  })
  it('names a Bot that asks and failed once, as one that asks', () => {
    expect(homeNames([bot('a', [1], 'failed')]).names).toEqual([{ slug: 'a', who: 'a', asks: 1, failed: false }])
  })
  it('names four at the most and counts the rest', () => {
    const got = homeNames([1, 2, 3, 4, 5, 6].map(i => bot(`b${i}`, [i])))
    expect(got.names).toHaveLength(4)
    expect(got.more).toBe(2)
  })
  it('has no name with nobody asking and nobody failed', () => {
    expect(homeNames([bot('a', []), bot('b', [], 'working')])).toEqual({ names: [], more: 0 })
  })
})

describe('placeNames', () => {
  const more = () => 0.05
  it('stands the names from the left with the gap between, and no two press areas overlap', () => {
    const got = placeNames([0.1, 0.2, 0.1], 0.04, more, -0.5, 0.5)
    expect(got.more).toBe(0)
    expect(got.at.map(a => +a.cx.toFixed(3))).toEqual([-0.45, -0.26, -0.07])
    for (let i = 1; i < got.at.length; i += 1) expect(got.at[i].cx - got.at[i].press / 2).toBeGreaterThanOrEqual(got.at[i - 1].cx + got.at[i - 1].press / 2 - 1e-9)
  })
  it('drops the names that do not fit and counts them with the rest', () => {
    const got = placeNames([0.2, 0.2, 0.2], 0.04, more, 0, 0.5, 1)
    expect(got.at).toHaveLength(1)
    expect(got.more).toBe(3)
    expect(got.moreAt + more()).toBeLessThanOrEqual(0.5)
  })
})

describe('a name keeps its place on the line (D75)', () => {
  it('stands the askers in the order they came, whatever their counts are now', () => {
    expect(homeNames([bot('juno', [1]), bot('atlas', [9])], 4, ['atlas', 'juno']).names.map(n => n.slug)).toEqual(['atlas', 'juno'])
  })
  it('puts a new asker after the askers that stand, and the failed after all askers', () => {
    const got = homeNames([bot('pico', [], 'failed'), bot('nova', [1, 2, 3]), bot('atlas', [9])], 4, ['atlas'])
    expect(got.names.map(n => n.slug)).toEqual(['atlas', 'nova', 'pico'])
  })
  it('lays a line with no order by most asks first', () => {
    expect(homeNames([bot('juno', [1]), bot('atlas', [9, 8])]).names.map(n => n.slug)).toEqual(['atlas', 'juno'])
  })
})

describe('a long name and a failed Bot on the line (D88 2)', () => {
  const b = (slug: string, asks: number, state = 'needs', label = slug) => ({ slug, label, state, needs: Array.from({ length: asks }, (_, i) => ({ ts: i })) }) as never
  // A sign is 10 wide here, "…" too; the room for a name is 100.
  const wide = (t: string) => signs(t).length * 10
  it('cuts a name to its width by whole signs, and leaves a short one whole', () => {
    expect(cutNames(['Juno of the Northern Rollout', 'Atlas'], wide, 100)).toEqual(['Juno of t…', 'Atlas'])
    // A flag is two code points and one sign: it is kept whole or left out whole.
    const flags = cutNames(['🇯🇵🇯🇵🇯🇵🇯🇵🇯🇵🇯🇵🇯🇵🇯🇵🇯🇵🇯🇵🇯🇵🇯🇵'], wide, 100)[0]
    expect(signs(flags)).toHaveLength(10)
    expect(flags.endsWith('…')).toBe(true)
    expect(cutNames(['東京リリース調整チームの担当ボット'], wide, 100)[0]).toBe('東京リリース調整チ…')
  })
  it('cuts two names that would read alike where they differ', () => {
    const [a, b] = cutNames(['Research Bot Alpha', 'Research Bot Beta'], wide, 100)
    expect(a).not.toBe(b)
    expect(a.endsWith('a') && a.includes('…')).toBe(true)
    expect(b.endsWith('Beta') || b.endsWith('eta') || b.endsWith('ta')).toBe(true)
    expect(wide(a)).toBeLessThanOrEqual(100)
  })
  it('folds an asker before a failed Bot, and the failed Bot only when one asker is left', () => {
    const n = (slug: string, failed = false) => ({ slug, who: slug, asks: failed ? 0 : 1, failed })
    expect(foldNext([n('a'), n('b'), n('c'), n('pico', true)])).toBe(2)
    expect(foldNext([n('a'), n('pico', true)])).toBe(1)
  })
  it('names three askers when three ask, then the failed Bot, and folds the fourth asker into "+n"', () => {
    const line = homeNames([b('a', 1), b('b', 1), b('c', 1), b('d', 1), b('e', 1), b('pico', 0, 'failed')])
    expect(line.names.map(n => n.slug)).toEqual(['a', 'b', 'c', 'pico'])
    expect(line.more).toBe(2)
  })
  it('folds a failed Bot only when the places after three askers are taken by failed Bots', () => {
    const line = fitLine([{ slug: 'a', who: 'a', asks: 1, failed: false }, { slug: 'x', who: 'x', asks: 0, failed: true }, { slug: 'y', who: 'y', asks: 0, failed: true }, { slug: 'z', who: 'z', asks: 0, failed: true }, { slug: 'w', who: 'w', asks: 0, failed: true }])
    expect(line.names.map(n => n.slug)).toEqual(['a', 'x', 'y', 'z'])
    expect(line.more).toBe(1)
  })
  it('names all when they fit', () => {
    expect(homeNames([b('a', 1), b('pico', 0, 'failed')])).toMatchObject({ more: 0 })
  })
})

