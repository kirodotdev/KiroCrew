/**
 * Widget height cache.
 *
 * The reserve a widget gets before its iframe reports is what decides whether a
 * card corrects on arrival. Two properties carry that:
 *
 *   * a HIT is exact, so there is no correction at all;
 *   * a MISS takes the MEDIAN OF ITS OWN KEY SPACE, so a never-seen widget
 *     starts at a typical height for that space.
 *
 * The space separation is the load-bearing part. Heights are only comparable
 * between widgets laid out at the same width: the artifacts thumbnail measures
 * at a fixed 900px base and clamps to 560, while a full-width frame measures at
 * its container's width and has no such ceiling. Let one space see the other's
 * samples and the thumbnail reserves frame-sized heights — the over-reserve that
 * produces the one-way drift the cache exists to remove.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const LS_KEY = 'mc-widget-heights'

/** Fresh module instance, since the cache loads from localStorage at import. */
async function loadModule(seed?: [string, number][]) {
  localStorage.clear()
  if (seed) localStorage.setItem(LS_KEY, JSON.stringify(seed))
  vi.resetModules()
  return import('../utils/widgetHeights')
}

describe('widgetHeights', () => {
  beforeEach(() => {
    vi.useRealTimers()
    localStorage.clear()
  })

  it('round-trips a measured height', async () => {
    const m = await loadModule()
    const k = m.widgetHeightKey('<p>hi</p>', 'thumb900')
    expect(m.getWidgetHeight(k)).toBeUndefined()
    m.setWidgetHeight(k, 231)
    expect(m.getWidgetHeight(k)).toBe(231)
  })

  it('keys the same content differently per space', async () => {
    const m = await loadModule()
    const html = '<p>same</p>'
    expect(m.widgetHeightKey(html, 'thumb900')).not.toBe(m.widgetHeightKey(html))
    // A space's key must be recognizable as belonging to it, since the median
    // filters by exactly that.
    expect(m.widgetHeightKey(html, 'thumb900').startsWith('thumb900:')).toBe(true)
  })

  it('keys different content differently within one space', async () => {
    const m = await loadModule()
    expect(m.widgetHeightKey('<p>a</p>', 'thumb900')).not.toBe(m.widgetHeightKey('<p>b</p>', 'thumb900'))
  })

  it('estimates from its OWN space only', async () => {
    const m = await loadModule()
    // Frame-space samples are tall; thumb-space samples are short.
    m.setWidgetHeight(m.widgetHeightKey('f1'), 1400)
    m.setWidgetHeight(m.widgetHeightKey('f2'), 1600)
    m.setWidgetHeight(m.widgetHeightKey('f3'), 1800)
    m.setWidgetHeight(m.widgetHeightKey('t1', 'thumb900'), 120)
    m.setWidgetHeight(m.widgetHeightKey('t2', 'thumb900'), 140)
    m.setWidgetHeight(m.widgetHeightKey('t3', 'thumb900'), 160)
    // Each space sees only its own median. Leaking either way is the defect.
    expect(m.estimateWidgetHeight('thumb900', 560)).toBe(140)
    expect(m.estimateWidgetHeight('', 200)).toBe(1600)
  })

  it('falls back when its own space has no samples, even if another does', async () => {
    const m = await loadModule()
    m.setWidgetHeight(m.widgetHeightKey('f1'), 1400)
    // A thumbnail on a cache with only frame samples must NOT inherit 1400.
    expect(m.estimateWidgetHeight('thumb900', 560)).toBe(560)
  })

  it('reads persisted heights back on a later load', async () => {
    const first = await loadModule()
    const k = first.widgetHeightKey('<p>persist</p>', 'thumb900')
    // Fake timers must be installed BEFORE the write, or the debounce timer is
    // created against the real clock and advancing the fake one never fires it.
    vi.useFakeTimers()
    first.setWidgetHeight(k, 222)
    vi.advanceTimersByTime(2000)
    vi.useRealTimers()
    const raw = localStorage.getItem(LS_KEY)
    expect(raw).toBeTruthy()
    // Simulate the next page load off the SAME storage: this is the warm-cache
    // path, the one that makes a revisit correct nothing at all.
    vi.resetModules()
    const second = await import('../utils/widgetHeights')
    expect(second.getWidgetHeight(k)).toBe(222)
  })

  it('survives unreadable storage instead of throwing', async () => {
    localStorage.setItem(LS_KEY, 'not json{{{')
    vi.resetModules()
    const m = await import('../utils/widgetHeights')
    expect(m.estimateWidgetHeight('thumb900', 560)).toBe(560)
    expect(() => m.setWidgetHeight('thumb900:1', 100)).not.toThrow()
  })

  it('ignores a repeat report of the same height', async () => {
    const m = await loadModule()
    const k = m.widgetHeightKey('<p>x</p>', 'thumb900')
    m.setWidgetHeight(k, 300)
    m.setWidgetHeight(k, 300)
    expect(m.getWidgetHeight(k)).toBe(300)
  })

  it('is the ONLY implementation writing its storage key', async () => {
    // Two modules each holding their own Map and each persisting a whole-map
    // overwrite of `mc-widget-heights` do not merely coexist — whichever flushes
    // last erases the other's fresh entries, so the warm-cache benefit silently
    // disappears for whichever surface lost the race. A page-scoped probe cannot
    // catch it either: it only ever sees one of the two consumers mount.
    const { readFileSync } = await import('node:fs')
    const { join } = await import('node:path')
    const read = (...p: string[]) => readFileSync(join(__dirname, '..', ...p), 'utf8')
    const util = read('utils', 'widgetHeights.ts')
    const frame = read('components', 'WidgetFrame.tsx')
    const page = read('pages', 'ArtifactsPage.tsx')
    // The key, the map, the debounce and the persist live in exactly one file.
    expect(util).toContain("'mc-widget-heights'")
    expect(frame).not.toContain('mc-widget-heights')
    expect(page).not.toContain('mc-widget-heights')
    // ...and both consumers go through it rather than keeping a private copy.
    for (const [name, text] of [['WidgetFrame', frame], ['ArtifactsPage', page]] as const) {
      expect(text, `${name} must import the shared cache`).toMatch(/from '\.\.\/utils\/widgetHeights'/)
      expect(text, `${name} must not hold its own map`).not.toMatch(/const heightCache/)
    }
    // The frame takes the DEFAULT key space, which is what keeps the entries it
    // already persisted resolvable across the extraction.
    expect(frame).toMatch(/widgetHeightKey\(html\)/)
  })
})
