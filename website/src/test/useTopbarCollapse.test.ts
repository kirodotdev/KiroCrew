/**
 * The desktop top bar's measured collapse ladder (lib/useTopbarCollapse.ts).
 *
 * happy-dom has no layout, so each test models it: the header's computed
 * `grid-template-columns` is a table indexed by the level currently applied to
 * it. That isolates the part this file owns -- which level the trial search
 * settles on -- from CSS grid's track sizing, which only a real engine can
 * answer (scripts/capture-topbar-flow.mjs sweeps that in Chromium).
 *
 * Budget used below: header 1048px wide, 12px padding each side and two 12px
 * gaps, so the three tracks fit when they sum to at most 1000px.
 */
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MOBILE_BREAKPOINT } from '../hooks/useIsMobile'
import {
  TOPBAR_CENTRED_UNTIL,
  TOPBAR_MAX_LEVEL,
  applyTopbarLevel,
  settleTopbarLevel,
  useTopbarCollapse,
} from '../lib/useTopbarCollapse'

type Tracks = [number, number, number]

/** A header whose resolved tracks are `tracksAt(level)` for the level applied. A
 *  raw string stands for an engine whose `grid-template-columns` does not resolve
 *  to px tracks. */
function fakeHeader(tracksAt: (level: number) => Tracks | string, width = 1048) {
  const el = document.createElement('header')
  Object.defineProperty(el, 'clientWidth', { configurable: true, get: () => width })
  const calls = { n: 0 }
  const real = globalThis.getComputedStyle
  vi.stubGlobal('getComputedStyle', (node: Element) => {
    if (node !== el) return real(node)
    calls.n++
    const tracks = tracksAt(Number(el.dataset.tbLevel))
    return {
      gridTemplateColumns: typeof tracks === 'string' ? tracks : tracks.map(t => `${t}px`).join(' '),
      columnGap: '12px',
      paddingLeft: '12px',
      paddingRight: '12px',
    } as unknown as CSSStyleDeclaration
  })
  return { el, calls }
}

const levelClasses = (el: HTMLElement) => [...el.classList].filter(c => c.startsWith('tbc-')).sort()
const upTo = (n: number) => Array.from({ length: n }, (_, i) => `tbc-${i + 1}`).sort()

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.replaceChildren()
})

describe('settleTopbarLevel', () => {
  it('climbs to the lowest level whose tracks fit', () => {
    // Levels 0-2 overflow by 40px; level 3 fits exactly.
    const { el } = fakeHeader(l => (l < 3 ? [400, 240, 400] : [380, 240, 380]))
    expect(settleTopbarLevel(el)).toBe(3)
    expect(levelClasses(el)).toEqual(upTo(3))
    expect(el.dataset.tbLevel).toBe('3')
  })

  it('keeps the search centred through the first rungs: an uneven fit is not a fit there', () => {
    // Fits by width at every level, but the sides stay 160px apart, so the search
    // sits off centre until the centring rungs are spent.
    const { el } = fakeHeader(() => [300, 240, 460])
    expect(settleTopbarLevel(el)).toBe(2)
  })

  it('climbs down from a stored level when the window widens, stopping at the centring rule', () => {
    // Everything fits centred from level 1; level 0 fits by width but not centred.
    const { el } = fakeHeader(l => (l === 0 ? [300, 240, 460] : [380, 240, 380]))
    applyTopbarLevel(el, 6)
    expect(settleTopbarLevel(el)).toBe(1)
    expect(levelClasses(el)).toEqual(upTo(1))
  })

  it('costs two reads when the stored level is still the right one', () => {
    // Level 2 fits, level 1 does not: re-settling at 2 reads level 2, tries 1, and
    // puts 2 back, touching no level above.
    const { el, calls } = fakeHeader(l => (l < 2 ? [400, 240, 400] : [380, 240, 380]))
    applyTopbarLevel(el, 2)
    expect(settleTopbarLevel(el)).toBe(2)
    expect(calls.n).toBe(2)
  })

  it('stops at the last rung when nothing fits', () => {
    const { el } = fakeHeader(() => [500, 240, 500])
    expect(settleTopbarLevel(el)).toBe(TOPBAR_MAX_LEVEL)
    expect(levelClasses(el)).toEqual(upTo(TOPBAR_MAX_LEVEL))
  })

  it('keeps every item on a header with no layout', () => {
    const { el, calls } = fakeHeader(() => [500, 240, 500], 0)
    expect(settleTopbarLevel(el)).toBe(0)
    expect(levelClasses(el)).toEqual([])
    expect(calls.n).toBe(0)
  })

  it('falls to the floor on a laid-out header whose tracks cannot be read', () => {
    // An engine that does not resolve the tracks to px: the floor clips at the
    // tail, where a stuck level 0 would overflow the header.
    for (const raw of ['none', 'auto auto auto']) {
      const { el } = fakeHeader(() => raw)
      expect(settleTopbarLevel(el), raw).toBe(TOPBAR_MAX_LEVEL)
      expect(levelClasses(el), raw).toEqual(upTo(TOPBAR_MAX_LEVEL))
      vi.unstubAllGlobals()
    }
  })

  it('treats an unreadable stored level as level 0', () => {
    const { el } = fakeHeader(() => [380, 240, 380])
    el.dataset.tbLevel = 'banana'
    expect(settleTopbarLevel(el)).toBe(0)
  })
})

describe('useTopbarCollapse', () => {
  it('settles on mount, re-settles when a group changes content, and clears when disabled', async () => {
    let pill = true // an update pill in the right group pushes the row to level 3
    const { el } = fakeHeader(l => (pill && l < 3 ? [400, 240, 400] : [380, 240, 380]))
    const right = document.createElement('div')
    right.className = 'tb-right'
    el.appendChild(right)
    document.body.appendChild(el)
    const ref = { current: el }

    const { result, rerender } = renderHook(({ on }) => useTopbarCollapse(ref, on), { initialProps: { on: true } })
    expect(result.current).toBe(3)

    // The pill unmounts. The group keeps its box when its track holds a share of
    // the spare room, so only the content mutation says a lower level fits again.
    await act(async () => {
      pill = false
      right.appendChild(document.createElement('span'))
      await new Promise(r => setTimeout(r, 0))
    })
    expect(result.current).toBe(0)
    expect(levelClasses(el)).toEqual([])

    pill = true
    rerender({ on: false })
    expect(result.current).toBe(0)
    expect(levelClasses(el)).toEqual([])
    expect(el.dataset.tbLevel).toBe('0')
  })
})

describe('index.css flow ladder', () => {
  const css = async () =>
    (await readFile(join(__dirname, '..', 'index.css'), 'utf8')).replace(/\/\*[\s\S]*?\*\//g, '')

  /** What each rung hides, cheapest first. */
  const LADDER = [
    '.tb-drop-metrics',
    '.tb-drop-feedback-label',
    '.tb-drop-navhistory',
    '.tb-drop-usage',
    '.tb-drop-feedback',
    '.tb-left .tb-drop-crew-name',
    // The capsule is a Liquid Glass host whose effect layers come first, so the
    // rung skips `[data-liquid-glass-layer]` on both sides, as the container
    // ladder's terminal rung does.
    '.tb-capsule > :not([data-liquid-glass-layer]) ~ :not([data-liquid-glass-layer])',
    '.tb-left .tb-crew-active-chip',
  ]

  it('hides one more group of items per rung, in order, for exactly the levels the hook steps through', async () => {
    const s = await css()
    const rules = [...s.matchAll(/\.tb-flow\.tbc-(\d+) ([^{]+)\{display:none\}/g)].map(m => [Number(m[1]), m[2].trim()])
    expect(rules).toEqual(LADDER.map((hides, i) => [i + 1, hides]))
    // Every level but the last hides something; the last is the floor below.
    expect(LADDER).toHaveLength(TOPBAR_MAX_LEVEL - 1)
    // The rungs spent to keep the search centred are the two cheapest: the metric
    // numbers and the feedback labels each leave an icon behind.
    expect(LADDER.slice(0, TOPBAR_CENTRED_UNTIL)).toEqual(['.tb-drop-metrics', '.tb-drop-feedback-label'])
    // The metrics rung's stand-in icon exists only once that rung has fired.
    expect(s).toMatch(/\.tb-flow:not\(\.tbc-1\) \.tb-narrow-only\{display:none\}/)
  })

  it('ends on a floor that clips each group instead of overflowing the header', async () => {
    // For content no rung hides (the open Windows menu, an extension widget): the
    // side tracks go back to equal halves, and the actions group clips from its
    // end, so its first item, the connection dot, is the last to go.
    const s = await css()
    const floor = s.match(new RegExp(`\\.topbar\\.tb-flow\\.tbc-${TOPBAR_MAX_LEVEL}\\{grid-template-columns:([^;}]+)\\}`))
    expect(floor, 'expected the last level to reset the side tracks').not.toBeNull()
    const parts = floor![1].trim().split(' ')
    expect([parts[0], parts[2]]).toEqual(['minmax(0,1fr)', 'minmax(0,1fr)'])
    expect(s).toMatch(new RegExp(`\\.topbar\\.tb-flow\\.tbc-${TOPBAR_MAX_LEVEL} > \\.tb-right\\{justify-content:safe flex-end\\}`))
    // The crews list's error notice takes only the identity track's spare room,
    // keeps its warning icon, and takes that room right after the switcher: the
    // switcher's own wrapper is flattened while the notice shows, so the pinned row
    // and the notice are siblings sharing the room. (Freezing the wrapper with
    // `flex-grow:0` instead starved the row, which only grows through it: every
    // pinned chip read as cut at any width.)
    expect(s).toMatch(/\.topbar\.tb-flow \.tb-left \.tb-crew-notice\{width:0;flex:100 1 auto;min-width:14px\}/)
    expect(s).toMatch(/\.topbar\.tb-flow \.tb-left \.tb-crew-grow:has\(> \.tb-crew-notice\) > \.tb-crew-grow\{display:contents\}/)
    expect(s).not.toMatch(/:has\(> \.tb-crew-notice\) > \.tb-crew-grow\{[^}]*flex-grow:0/)
  })

  it('lets pinned chips go icon-only once their names are hidden', async () => {
    // A connected chip keeps a 5ch floor for its name (InstanceTabBar.tsx); with
    // the name hidden at rung 6 that floor would only hold blank width.
    const s = await css()
    expect(s).toMatch(/\.tb-flow\.tbc-6 \.tb-left \.crew-chip-row > \*\{min-width:auto\}/)
  })

  it('scopes the flow rules to the width App.tsx enables the ladder at', async () => {
    // App.tsx enables the hook exactly when useIsMobile is false.
    const s = await css()
    expect(s).toMatch(new RegExp(`@media \\(min-width:${MOBILE_BREAKPOINT}px\\)\\{\\s*\\.topbar\\.tb-flow\\{`))
  })
})
