import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { frecencyScore, recordUse, _clearUsageForTest, type UsageMap } from './frecency'
import { rankRootRows, type RootRow } from './rootIndex'

/**
 * Unit tests for the Command Bar root index and its frecency ranking.
 *
 * The root is the surface whose cost the whole design rests on, so two of these
 * are ratchets rather than behaviour checks: the root must stay purely local, and
 * a group must not be able to crowd out the others.
 */

function row(over: Partial<RootRow> = {}): RootRow {
  return { id: over.title ?? 'r', title: 'Row', group: 'commands', kind: 'invoke', ...over }
}

const DAY = 24 * 60 * 60 * 1000

describe('frecencyScore', () => {
  it('scores an unused row zero so a first-time row ranks on match quality alone', () => {
    expect(frecencyScore(undefined)).toBe(0)
    expect(frecencyScore({ count: 0, last: Date.now() })).toBe(0)
  })

  it('halves a use every 14 days', () => {
    const now = 100 * DAY
    const fresh = frecencyScore({ count: 1, last: now }, now)
    const aged = frecencyScore({ count: 1, last: now - 14 * DAY }, now)
    expect(fresh).toBeCloseTo(1, 6)
    expect(aged).toBeCloseTo(0.5, 6)
  })

  it('prefers frequency at equal recency and recency at equal frequency', () => {
    const now = 100 * DAY
    expect(frecencyScore({ count: 5, last: now }, now)).toBeGreaterThan(
      frecencyScore({ count: 1, last: now }, now),
    )
    expect(frecencyScore({ count: 3, last: now }, now)).toBeGreaterThan(
      frecencyScore({ count: 3, last: now - 30 * DAY }, now),
    )
  })
})

describe('recordUse', () => {
  beforeEach(() => _clearUsageForTest())

  it('increments and persists', () => {
    const once = recordUse('a', 1000)
    expect(once['a']).toEqual({ count: 1, last: 1000 })
    const twice = recordUse('a', 2000, once)
    expect(twice['a']).toEqual({ count: 2, last: 2000 })
  })

  it('survives a storage that throws', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    expect(() => recordUse('a', 1000)).not.toThrow()
    setItem.mockRestore()
  })
})

describe('rankRootRows', () => {
  it('orders an empty query by frecency, not alphabetically', () => {
    const now = 10 * DAY
    const rows = [row({ id: 'z', title: 'Zebra' }), row({ id: 'a', title: 'Apple' })]
    const usage: UsageMap = { z: { count: 9, last: now } }
    expect(rankRootRows(rows, '', usage, now).map(r => r.id)).toEqual(['z', 'a'])
  })

  it('drops rows the query cannot match', () => {
    const rows = [row({ id: 'a', title: 'Restart Dev Fleet' }), row({ id: 'b', title: 'Settings' })]
    expect(rankRootRows(rows, 'restart', {}, 0).map(r => r.id)).toEqual(['a'])
  })

  it('returns highlight indices for a title match and none for an alias match', () => {
    const rows = [row({ id: 'a', title: 'Restart', keywords: ['reboot'] })]
    expect(rankRootRows(rows, 'res', {}, 0)[0].indices).toEqual([0, 1, 2])
    expect(rankRootRows(rows, 'reboot', {}, 0)[0].indices).toEqual([])
  })

  it('lets habit break a near tie but not beat a clearly better match', () => {
    const now = 10 * DAY
    const rows = [
      row({ id: 'used', title: 'Search Sessions' }),
      row({ id: 'exact', title: 'Se' }),
    ]
    const usage: UsageMap = { used: { count: 4, last: now } }
    // Exact-match on a never-used row still wins.
    expect(rankRootRows(rows, 'se', usage, now)[0].id).toBe('exact')
    // With no exact competitor, the used row leads.
    const near = [row({ id: 'used', title: 'Search Sessions' }), row({ id: 'cold', title: 'Search Skills' })]
    expect(rankRootRows(near, 'search s', usage, now)[0].id).toBe('used')
  })

  it('caps each group so one group cannot crowd out the others', () => {
    const many: RootRow[] = []
    for (let i = 0; i < 20; i++) many.push(row({ id: `app${i}`, title: `App ${i}`, group: 'apps' }))
    many.push(row({ id: 'cmd', title: 'App Command', group: 'commands' }))
    const out = rankRootRows(many, 'app', {}, 0)
    expect(out.filter(r => r.group === 'apps')).toHaveLength(6)
    expect(out.some(r => r.id === 'cmd')).toBe(true)
  })

  it('returns group blocks in launcher order, not in score order', () => {
    // What a launcher leads with is a product decision, so it must not depend on
    // which row happens to score highest — with no usage every score ties and the
    // tiebreak is alphabetical, which floated settings above the commands.
    const rows = [
      row({ id: 's', title: 'Attach screenshots', group: 'settings' }),
      row({ id: 'a', title: 'Beta app', group: 'apps' }),
      row({ id: 'c', title: 'Yankee command', group: 'commands' }),
    ]
    expect(rankRootRows(rows, '', {}, 0).map(r => r.group)).toEqual([
      'commands',
      'apps',
      'settings',
    ])
  })

  it('keeps ranking order inside a group', () => {
    const now = 10 * DAY
    const rows = [
      row({ id: 'cold', title: 'App Cold', group: 'apps' }),
      row({ id: 'hot', title: 'App Hot', group: 'apps' }),
    ]
    const usage: UsageMap = { hot: { count: 5, last: now } }
    expect(rankRootRows(rows, '', usage, now).map(r => r.id)).toEqual(['hot', 'cold'])
  })

  it('holds settings back on an empty query but not on a real one', () => {
    const rows: RootRow[] = []
    for (let i = 0; i < 8; i++) {
      rows.push(row({ id: `set${i}`, title: `Setting ${i}`, group: 'settings' }))
    }
    // Idle: settings are a searchable tail, not what the bar opens on.
    expect(rankRootRows(rows, '', {}, 0)).toHaveLength(2)
    // Typed: they rank normally, up to the usual per-group cap.
    expect(rankRootRows(rows, 'setting', {}, 0)).toHaveLength(6)
  })
})

describe('root purity ratchet', () => {
  it('the root index reaches no backend', () => {
    // The design guarantee is structural, not a matter of care: if the root ever
    // gains a fetch, typing in it starts costing a request per keystroke again —
    // the exact regression this surface exists to remove.
    const src = readFileSync(join(__dirname, 'rootIndex.ts'), 'utf8')
    for (const forbidden of ['fetch(', 'api.', 'useQuery', 'axios']) {
      expect(src).not.toContain(forbidden)
    }
  })

  it('the overlay ranks the root from the live query, never the debounced one', () => {
    // Debounce belongs to the network path only. Ranking the local root through it
    // means two keystrokes then Enter -- a launcher's whole point -- activates the
    // row that was selected against the PREVIOUS query, and the fallback row is not
    // on screen yet either.
    const src = readFileSync(join(__dirname, 'CommandBarOverlay.tsx'), 'utf8')
    expect(src).toContain('rankRootRows(rootRows, query, usage)')
    expect(src).not.toMatch(/rankRootRows\(rootRows,\s*debounced/)
    // The fallback row rides the same live query. It used to be a `fallbackVisible`
    // boolean; it is now a slot pushed under that condition, so the condition is what
    // this pins rather than the name it was held in.
    expect(src).toMatch(/if \(query\.trim\(\)\.length > 0\)/)
    expect(src).toMatch(/tag: 'fallback'/)
    expect(src).not.toMatch(/if \(debounced\.trim\(\)\.length > 0\)/)
  })

  it('backs its aria-modal promise with a real focus trap', () => {
    // Declaring aria-modal without trapping Tab tells a keyboard or screen-reader
    // user that the page behind is unreachable when it is not, so the claim and the
    // trap have to travel together on the same element.
    const src = readFileSync(join(__dirname, 'CommandBarOverlay.tsx'), 'utf8')
    expect(src).toMatch(/useDialogFocusTrap\(dialogRef,/)
    const dialog = src.slice(src.indexOf('ref={dialogRef}'))
    // The window is generous because the props it spans carry their own reasoning in
    // comments; what is being asserted is that `aria-modal` sits on the SAME element
    // as the trap ref, not that it is within N characters of it.
    expect(dialog.slice(0, 1600)).toContain('aria-modal="true"')
  })
})
