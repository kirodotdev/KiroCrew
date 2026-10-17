/**
 * The desktop top bar's measured collapse ladders (lib/useTopbarCollapse.ts).
 *
 * happy-dom has no layout, so each test models it: a group's border box is its
 * track width, except while the hook sizes it to `max-content` for a read, when
 * it is a width looked up from the level currently applied to that group. That
 * isolates the part this file owns -- which level each trial search settles on
 * -- from flex and grid sizing, which only a real engine can answer
 * (scripts/capture-topbar-flow.mjs sweeps that in Chromium).
 */
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { MOBILE_BREAKPOINT } from '../hooks/useIsMobile'
import {
  TOPBAR_CONTAINER_FOLDS,
  TOPBAR_LEVELS,
  applyTopbarLevel,
  settleTopbarSide,
  useTopbarCollapse,
  type TopbarSide,
} from '../lib/useTopbarCollapse'

const SIDES: TopbarSide[] = ['left', 'right']
const DATA = { left: 'tbLeft', right: 'tbRight' } as const

/** An element with `className`, optionally carrying children. */
function el(tag: string, className: string, children: HTMLElement[] = [], attrs: Record<string, string> = {}): HTMLElement {
  const node = document.createElement(tag)
  if (className) node.className = className
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v)
  node.append(...children)
  return node
}

/** The items each container rung hides, keyed by the measured step it shares.
 *  The capsule's rung hides every non-layer child after the first, so it gets a
 *  layer, the dot and one readout. */
const TARGETS: Record<TopbarSide, [number, () => HTMLElement][]> = {
  left: [
    [1, () => el('span', 'tb-drop-navhistory')],
    [2, () => el('span', 'tb-drop-crew-name')],
    [3, () => el('span', 'tb-crew-active-chip')],
  ],
  right: [
    [1, () => el('span', 'tb-drop-metrics')],
    [3, () => el('span', 'tb-drop-usage')],
    [4, () => el('span', 'tb-drop-feedback', [el('span', 'tb-drop-feedback-label')])],
    [5, () => el('div', 'tb-capsule', [el('span', '', [], { 'data-liquid-glass-layer': '' }), el('span', ''), el('span', '')])],
  ],
}

/** A header with both groups. `need[side](level)` is that group's max-content
 *  width at the level applied to it, and `box[side]` its border-box width.
 *  Each group's wrapper holds the container rungs' targets; a step in
 *  `folded[side]` gives that step's elements no box, as its container rung
 *  firing would. */
function fakeBar(
  need: Partial<Record<TopbarSide, (level: number) => number>>,
  box: Record<TopbarSide, number> = { left: 400, right: 400 },
  padding: Partial<Record<TopbarSide, { left: number; right: number }>> = {},
) {
  const header = document.createElement('header')
  const groups = {} as Record<TopbarSide, HTMLElement>
  const wrappers = {} as Record<TopbarSide, HTMLElement>
  const reads = { left: 0, right: 0 }
  const groupReads = { left: 0, right: 0 }
  const folded = { left: new Set<number>(), right: new Set<number>() }
  for (const side of SIDES) {
    const g = document.createElement('div')
    const wrapper = document.createElement('div')
    g.className = `tb-${side}`
    wrapper.className = 'tb-measure'
    if (padding[side]) {
      g.style.paddingLeft = `${padding[side].left}px`
      g.style.paddingRight = `${padding[side].right}px`
    }
    g.getBoundingClientRect = () => {
      groupReads[side]++
      return { width: box[side] } as DOMRect
    }
    wrapper.getBoundingClientRect = () => {
      if (wrapper.style.width !== 'max-content') return { width: 0 } as DOMRect
      reads[side]++
      return { width: need[side]?.(Number(header.dataset[DATA[side]])) ?? 0 } as DOMRect
    }
    for (const [step, make] of TARGETS[side]) {
      const item = make()
      for (const node of [item, ...item.querySelectorAll<HTMLElement>('*')]) {
        node.getClientRects = () => (folded[side].has(step) ? [] : [new DOMRect(0, 0, 1, 1)]) as unknown as DOMRectList
      }
      wrapper.appendChild(item)
    }
    g.appendChild(wrapper)
    header.appendChild(g)
    groups[side] = g
    wrappers[side] = wrapper
  }
  return { header, groups, wrappers, reads, groupReads, folded }
}

const levelClasses = (el: HTMLElement) => [...el.classList].filter(c => /^tb[lr]-/.test(c)).sort()
const upTo = (prefix: string, n: number) => Array.from({ length: n }, (_, i) => `${prefix}-${i + 1}`)

afterEach(() => {
  document.body.replaceChildren()
})

describe('settleTopbarSide', () => {
  it('climbs each group to its own lowest fitting level', () => {
    // The identity group overflows its 400px track until level 2; the actions
    // group fits at level 0. Neither level touches the other group.
    const { header } = fakeBar({ left: l => (l < 2 ? 420 : 380), right: () => 300 })
    expect(settleTopbarSide(header, 'left')).toBe(2)
    expect(settleTopbarSide(header, 'right')).toBe(0)
    expect(levelClasses(header)).toEqual(upTo('tbl', 2))
    expect(header.dataset.tbLeft).toBe('2')
    expect(header.dataset.tbRight).toBe('0')
  })

  it('measures the wrapper while reading the group box only once', () => {
    const { header, reads, groupReads } = fakeBar({ right: l => (l < 2 ? 420 : 380) })
    expect(settleTopbarSide(header, 'right')).toBe(2)
    expect(reads.right).toBeGreaterThan(0)
    expect(groupReads.right).toBe(1)
  })

  it('counts a group that fits exactly as fitting', () => {
    const { header } = fakeBar({ right: l => (l < 3 ? 401 : 400) })
    expect(settleTopbarSide(header, 'right')).toBe(3)
  })

  it('subtracts group padding from the available content box', () => {
    const { header } = fakeBar(
      { right: l => (l === 0 ? 400 : 380) },
      { left: 400, right: 400 },
      { right: { left: 0, right: 6 } },
    )
    document.body.appendChild(header)
    expect(settleTopbarSide(header, 'right')).toBe(1)
  })

  it('climbs down from a stored level when the window widens', () => {
    const { header } = fakeBar({ right: l => (l < 1 ? 420 : 380) })
    applyTopbarLevel(header, 'right', TOPBAR_LEVELS.right)
    expect(settleTopbarSide(header, 'right')).toBe(1)
    expect(levelClasses(header)).toEqual(upTo('tbr', 1))
  })

  it('costs two reads when the stored level is still the right one', () => {
    // Level 2 fits, level 1 does not: re-settling at 2 reads level 2, tries 1, and
    // puts 2 back, touching no level above.
    const { header, reads } = fakeBar({ right: l => (l < 2 ? 420 : 380) })
    applyTopbarLevel(header, 'right', 2)
    expect(settleTopbarSide(header, 'right')).toBe(2)
    expect(reads.right).toBe(2)
  })

  it('stops at the last rung when nothing fits, and clips there', () => {
    const { header } = fakeBar({ left: () => 900, right: () => 900 })
    for (const side of SIDES) expect(settleTopbarSide(header, side)).toBe(TOPBAR_LEVELS[side])
    expect(levelClasses(header)).toEqual([...upTo('tbl', TOPBAR_LEVELS.left), ...upTo('tbr', TOPBAR_LEVELS.right)].sort())
  })

  it('keeps every item in a group with no box', () => {
    const { header, reads } = fakeBar({ right: () => 900 }, { left: 0, right: 0 })
    applyTopbarLevel(header, 'right', 3)
    expect(settleTopbarSide(header, 'right')).toBe(0)
    expect(levelClasses(header)).toEqual([])
    expect(reads.right).toBe(0)
  })

  it('keeps every item in a group without a measurement wrapper', () => {
    const header = document.createElement('header')
    const group = document.createElement('div')
    group.className = 'tb-right'
    group.getBoundingClientRect = () => ({ width: group.style.width === 'max-content' ? 900 : 400 }) as DOMRect
    header.appendChild(group)
    applyTopbarLevel(header, 'right', 3)
    expect(settleTopbarSide(header, 'right')).toBe(0)
    expect(levelClasses(header)).toEqual([])
  })

  it('restores the wrapper inline style exactly and never writes the group style', () => {
    const { header, groups, wrappers } = fakeBar({ right: l => (l < 2 ? 420 : 380) })
    groups.right.style.position = 'relative'
    wrappers.right.style.cssText = 'color: red; order: 2;'
    const savedWrapperStyle = wrappers.right.style.cssText
    const savedGroupStyle = groups.right.style.cssText
    const observer = new MutationObserver(() => {})
    observer.observe(groups.right, { attributes: true, attributeFilter: ['style'] })
    settleTopbarSide(header, 'right')
    expect(wrappers.right.style.cssText).toBe(savedWrapperStyle)
    expect(groups.right.style.cssText).toBe(savedGroupStyle)
    expect(observer.takeRecords()).toHaveLength(0)
    observer.disconnect()
  })

  it('treats an unreadable stored level as level 0', () => {
    const { header } = fakeBar({ left: () => 300 })
    header.dataset.tbLeft = 'banana'
    expect(settleTopbarSide(header, 'left')).toBe(0)
  })

  it('stops descending at the last step the container rungs have folded', () => {
    // The credits rung has fired, so at level 0 the labels would come back while
    // credits stay folded: the level holds at 3.
    const { header, folded, reads } = fakeBar({ right: () => 300 })
    folded.right.add(3)
    applyTopbarLevel(header, 'right', 3)
    expect(settleTopbarSide(header, 'right')).toBe(3)
    expect(levelClasses(header)).toEqual(upTo('tbr', 3))
    // Two reads: level 3, then the trial of level 2 that the folded step stops.
    // Descending to 0 and raising again would reach the same level in four.
    expect(reads.right).toBe(2)
  })

  it('raises a fitting level to the last step the container rungs have folded', () => {
    for (const [step, want] of [[3, 3], [4, 4], [5, 5]] as const) {
      const { header, folded } = fakeBar({ right: () => 300 })
      folded.right.add(step)
      expect(settleTopbarSide(header, 'right'), `step ${step}`).toBe(want)
      expect(header.dataset.tbRight).toBe(String(want))
    }
  })

  it('takes no floor from a step without a container rung', () => {
    // The feedback labels have no container rung: a label without a box is not
    // a fold the measured level has to follow.
    const { header, wrappers } = fakeBar({ right: () => 300 })
    const label = wrappers.right.querySelector<HTMLElement>('.tb-drop-feedback-label')!
    label.getClientRects = () => [] as unknown as DOMRectList
    expect(settleTopbarSide(header, 'right')).toBe(0)
    label.remove()
    expect(settleTopbarSide(header, 'right')).toBe(0)
  })

  it('keeps a fitting level that is already above the container floor', () => {
    const { header, folded } = fakeBar({ right: l => (l < 5 ? 420 : 380) })
    folded.right.add(3)
    expect(settleTopbarSide(header, 'right')).toBe(5)
  })
})

describe('useTopbarCollapse', () => {
  it('re-settles on mount, on a group gaining an element, on a text change and on a class change, and clears when disabled', async () => {
    // Each rung saves the actions group 40px against its 400px track: it starts
    // at level 1, an element mounting pushes it to 2, and its text growing to 3.
    let need = 420
    const { header, groups } = fakeBar({ right: l => need - 40 * l })
    document.body.appendChild(header)
    const label = document.createTextNode('Downloading 5%')
    groups.right.appendChild(label)
    const ref = { current: header }
    const flush = () => new Promise(r => setTimeout(r, 0))

    const { result, rerender } = renderHook(({ on }) => useTopbarCollapse(ref, on), { initialProps: { on: true } })
    expect(result.current).toEqual({ left: 0, right: 1 })

    // The group's box is fixed by its track, so a pill mounting does not resize
    // it: only the content mutation says the level moved.
    await act(async () => {
      need = 460
      groups.right.appendChild(document.createElement('span'))
      await flush()
    })
    expect(result.current.right).toBe(2)

    // A text-only change, such as the pill's progress ticking over.
    await act(async () => {
      need = 500
      label.data = 'Downloading 100%'
      await flush()
    })
    expect(result.current.right).toBe(3)

    // The group's own class: `tb-has-update` shifts the container rungs, and
    // with them the floor, before the pill's chunk has mounted anything.
    await act(async () => {
      need = 540
      groups.right.classList.add('tb-has-update')
      await flush()
    })
    expect(result.current.right).toBe(4)

    rerender({ on: false })
    expect(result.current).toEqual({ left: 0, right: 0 })
    expect(levelClasses(header)).toEqual([])
    expect(header.dataset.tbRight).toBe('0')
  })

  it('re-settles when the font family or theme on <html> changes, and stops when unmounted', async () => {
    // Neither change resizes a group box or adds a node to one; each only
    // changes how wide the same text is.
    let need = 420
    const { header } = fakeBar({ right: l => need - 40 * l })
    document.body.appendChild(header)
    const ref = { current: header }
    const root = document.documentElement
    const saved = { style: root.getAttribute('style'), font: root.dataset.fontFamily, theme: root.dataset.theme }
    const flush = () => new Promise(r => setTimeout(r, 0))

    const { result, unmount } = renderHook(() => useTopbarCollapse(ref, true))
    expect(result.current.right).toBe(1)

    try {
      await act(async () => {
        need = 460
        root.style.setProperty('--font-body', '"Wide Font", sans-serif')
        await flush()
      })
      expect(result.current.right).toBe(2)

      await act(async () => {
        need = 500
        root.dataset.fontFamily = 'wide'
        await flush()
      })
      expect(result.current.right).toBe(3)

      await act(async () => {
        need = 540
        root.dataset.theme = 'kiro-dark'
        await flush()
      })
      expect(result.current.right).toBe(4)

      unmount()
      need = 420
      root.dataset.theme = 'light'
      await flush()
      expect(header.dataset.tbRight).toBe('4')
    } finally {
      if (saved.style === null) root.removeAttribute('style'); else root.setAttribute('style', saved.style)
      if (saved.font === undefined) delete root.dataset.fontFamily; else root.dataset.fontFamily = saved.font
      if (saved.theme === undefined) delete root.dataset.theme; else root.dataset.theme = saved.theme
    }
  })
})

describe('index.css measured ladders', () => {
  const css = async () =>
    (await readFile(join(__dirname, '..', 'index.css'), 'utf8')).replace(/\/\*[\s\S]*?\*\//g, '')

  /** What each rung hides, cheapest first, per group. */
  const LADDERS: Record<TopbarSide, string[]> = {
    left: ['.tb-left .tb-drop-navhistory', '.tb-left .tb-drop-crew-name', '.tb-left .tb-crew-active-chip'],
    right: [
      '.tb-right .tb-drop-metrics',
      '.tb-right .tb-drop-feedback-label',
      '.tb-right .tb-drop-usage',
      '.tb-right .tb-drop-feedback',
      // The capsule is a Liquid Glass host whose effect layers come first, so the
      // rung skips `[data-liquid-glass-layer]` on both sides, as the container
      // ladder's terminal rung does.
      '.tb-right .tb-capsule > :not([data-liquid-glass-layer]) ~ :not([data-liquid-glass-layer])',
      // Last, so the update pill's label cannot push the bell out of the group.
      '.tb-right .tb-drop-update-label',
    ],
  }

  it('hides one more group of items per rung, in order, for exactly the levels the hook steps through', async () => {
    const s = await css()
    for (const side of SIDES) {
      const prefix = side === 'left' ? 'tbl' : 'tbr'
      const rules = [...s.matchAll(new RegExp(`\\.tb-measured\\.${prefix}-(\\d+) ([^{]+)\\{display:none\\}`, 'g'))]
        .map(m => [Number(m[1]), m[2].trim()])
      expect(rules, side).toEqual(LADDERS[side].map((hides, i) => [i + 1, hides]))
      expect(LADDERS[side], side).toHaveLength(TOPBAR_LEVELS[side])
    }
    // The metrics rung's stand-in icon is shown when that measured rung hides
    // the numbers; otherwise the live container-query rules own its visibility.
    expect(s).toMatch(/\.tb-measured\.tbr-1 \.tb-right \.tb-narrow-only\{display:block\}/)
    expect(s).not.toMatch(/\.tb-measured:not\(\.tbr-1\) \.tb-right \.tb-narrow-only\{display:none\}/)
    expect(s).not.toMatch(/container-type:\s*normal/)

    const wrapperRule = '.tb-left > .tb-measure,.tb-right > .tb-measure{display:contents}'
    const wrapperRuleAt = s.indexOf(wrapperRule)
    expect(wrapperRuleAt, 'expected the layout-neutral measurement wrapper rule').toBeGreaterThanOrEqual(0)
    const braceDepth = [...s.slice(0, wrapperRuleAt)].reduce(
      (depth, char) => depth + (char === '{' ? 1 : char === '}' ? -1 : 0),
      0,
    )
    expect(braceDepth, 'measurement wrapper rule must be outside every media query').toBe(0)
  })

  it('collapses in the order the phone container ladder uses', async () => {
    // Each item that also has a base container rung keeps its place: thresholds
    // fall along each desktop ladder. (The feedback labels have no container rung.)
    const s = await css()
    const threshold = new Map(
      [...s.matchAll(/^@container \(max-width:(\d+)px\)\{ ?([^{]+)\{display:none\} ?\}/gm)].map(m => [m[2].trim(), Number(m[1])]),
    )
    for (const side of SIDES) {
      const order = LADDERS[side]
        .map(sel => threshold.get(sel) ?? threshold.get(sel.replace(/^\.tb-right /, '')))
        .filter((t): t is number => t !== undefined)
      expect(order.length, side).toBeGreaterThanOrEqual(3)
      expect(order, side).toEqual([...order].sort((a, b) => b - a))
    }
  })

  it('names, for every step the hook reads a container fold from, a target a container rung hides', async () => {
    // The hook's floor (TOPBAR_CONTAINER_FOLDS) is only right while each selector
    // is one the `@container` rungs hide, at the step it shares in LADDERS.
    const s = await css()
    const targets = new Set(
      [...s.matchAll(/@container \(max-width:\d+px\)\{ ?([^{]+)\{display:none\} ?\}/g)].map(m =>
        m[1].trim().replace(/^\.tb-(left|right|has-update) /, ''),
      ),
    )
    for (const side of SIDES) {
      const folds = Object.entries(TOPBAR_CONTAINER_FOLDS[side])
      expect(folds.length, side).toBeGreaterThanOrEqual(3)
      for (const [step, sel] of folds) {
        expect(targets, `${side} ${step}`).toContain(sel)
        expect(LADDERS[side][Number(step) - 1], `${side} ${step}`).toBe(`.tb-${side} ${sel}`)
      }
    }
  })

  it('reads a container fold for every item a container rung hides', async () => {
    // The reverse direction: a container rung with no entry in
    // TOPBAR_CONTAINER_FOLDS could fold an item the measured level does not
    // follow, and the two ladders would fold out of order again. Every rung target
    // is some side's entry, under the side the rung scopes it to (a rung without a
    // `.tb-left`/`.tb-right` scope hides a desktop readout, which is the actions
    // group's).
    const s = await css()
    const known = Object.fromEntries(SIDES.map(side => [side, new Set(Object.values(TOPBAR_CONTAINER_FOLDS[side]))]))
    const rungs = [...s.matchAll(/@container \(max-width:\d+px\)\{ ?([^{]+)\{display:none\} ?\}/g)].map(m => m[1].trim())
    expect(rungs.length).toBeGreaterThanOrEqual(7)
    for (const rung of rungs) {
      const scoped = rung.match(/^\.tb-(left|right) (.+)$/)
      const side: TopbarSide = scoped ? (scoped[1] as TopbarSide) : 'right'
      const sel = (scoped ? scoped[2] : rung).replace(/^\.tb-has-update /, '')
      expect(known[side], `rung "${rung}" has no TOPBAR_CONTAINER_FOLDS entry`).toContain(sel)
    }
  })

  it('keeps the search centred: the desktop form sets no track list of its own', async () => {
    // The side tracks stay the equal `minmax(0,1fr)` pair, so the search sits on
    // the header's centre line at every width. The actions group aligns `safe` to
    // its end, so if it overflows at its last rung it clips from its far end and
    // its first item, the connection dot, goes last.
    const s = await css()
    expect(s).not.toMatch(/\.tb-measured[^{]*\{[^}]*grid-template-columns/)
    expect(s).toMatch(/\.topbar\.tb-measured > \.tb-right\{justify-content:safe flex-end\}/)
  })

  it('lets the crews list error notice give way by its message only', async () => {
    // The notice takes only the identity group's spare room and takes that room
    // right after the switcher: the switcher's own wrapper is flattened while the
    // notice shows, so the pinned row and the notice are siblings sharing the
    // room. (Freezing the wrapper with `flex-grow:0` instead starved the row,
    // which only grows through it: every pinned chip read as cut at any width.)
    // Its floor is its own max-content, so the hand-off's label cannot wrap in
    // the floor calculation, and only the message is allowed to shrink inside it
    // (`width:0` adds nothing to that max-content), so the warning icon and the
    // unwrapped hand-off button stay whole and are never clipped while still
    // focusable; the message alone gives way. The 4px end margin keeps the
    // hand-off's focus ring (2px outline plus 2px offset) inside the group's clip
    // when the notice ends flush with it.
    const s = await css()
    expect(s).toMatch(/\.topbar\.tb-measured \.tb-left \.tb-crew-notice\{width:0;flex:100 1 auto;min-width:max-content;margin-right:4px\}/)
    expect(s).not.toMatch(/\.tb-crew-notice\{[^}]*min-width:14px/)
    expect(s).not.toMatch(/\.tb-crew-notice\{[^}]*min-content/)
    expect(s).toMatch(/\.topbar\.tb-measured \.tb-left \.tb-crew-notice-msg\{width:0;flex:1 1 auto;max-width:max-content\}/)
    expect(s).toMatch(/\.topbar\.tb-measured \.tb-left \.tb-crew-grow:has\(> \.tb-crew-notice\) > \.tb-crew-grow\{display:contents\}/)
    expect(s).not.toMatch(/:has\(> \.tb-crew-notice\) > \.tb-crew-grow\{[^}]*flex-grow:0/)
  })

  it('folds the metrics notice to its icons at the capsule rung instead of letting it give way', async () => {
    // The notice's text is short and fixed, so it keeps its natural width and
    // the actions ladder makes room for it. Rung tbr-5 hides its message visually
    // (the sr-only set keeps the alert text) and collapses its hand-off label
    // (font-size:0 keeps the button named). Rung tbr-2 leaves it whole: a bare
    // icon there would have no visible name on keyboard focus.
    const s = await css()
    expect(s).toMatch(/\.tb-measured\.tbr-5 \.tb-right \.tb-metrics-notice button\{font-size:0;gap:0\}/)
    expect(s).toMatch(/\.tb-measured\.tbr-5 \.tb-right \.tb-metrics-notice-msg\{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect\(0,0,0,0\);white-space:nowrap;border-width:0\}/)
    expect(s).not.toMatch(/\.tbr-[1-4] [^{]*\.tb-metrics-notice/)
    expect(s).not.toMatch(/\.tb-metrics-notice\{[^}]*(width:0|overflow:hidden)/)
  })

  it('lets pinned chips go icon-only once their names are hidden', async () => {
    // A connected chip keeps a 5ch floor for its name (InstanceTabBar.tsx); with
    // the name hidden at rung tbl-2 that floor would only hold blank width.
    const s = await css()
    expect(s).toMatch(/\.tb-measured\.tbl-2 \.tb-left \.crew-chip-row > \*\{min-width:auto\}/)
  })

  it('scopes the desktop rules to the width App.tsx enables the ladders at', async () => {
    // App.tsx enables the hook exactly when useIsMobile is false.
    const s = await css()
    expect(s).toMatch(new RegExp(`@media \\(min-width:${MOBILE_BREAKPOINT}px\\)\\{\\s*\\.topbar\\.tb-measured > `))
  })
})
