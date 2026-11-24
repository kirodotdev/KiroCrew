/**
 * Who needs him, by name, on Home's strip (D65, D69): one line under the four counts, "Atlas 2 · Juno 1 · Pico
 * failed". The Bots that ask stand first, the one with the most asks first; then the Bots that failed. Four names at
 * the most, and the rest as "+n". With nobody asking and nobody failed there is no line. Plain rules: `Home.tsx`
 * draws the line and `useHomeNames.ts` says what a pinch on a name does.
 */
import type { XrBot } from '../model'

/** One name of the line: the Bot, how many asks of it wait, and whether it stands there as failed. */
/** `gone`: it left the line while his dot was on the line; its place is kept empty until the dot is off (D75). */
export interface HomeName { slug: string; who: string; asks: number; failed: boolean; gone?: boolean }

export const HOME_NAMES = {
  /** The most names; the rest is counted as "+n". */
  most: 4,
  /**
   * How high the line's letters are set (metres, the font's size). At the strip's distance a capital letter of it
   * is 0.6 degrees high, over the 0.53 degrees that a page of two in view reads at (D66).
   */
  textM: 0.03,
  /** The strip's two rows while the line stands: the counts' row and the names' row share the strip's own height. */
  countsRowM: 0.044,
  rowM: 0.041,
  /** A name's most width on the line (about ten Latin letters); wider, it is cut and ends in "…". And the askers named before any failed Bot. */
  nameMaxM: 0.19,
  askersFirst: 3,
  /** The counts' digits while the line stands under them (0.05 with no line). */
  digitM: 0.038,
  /** The air between two names, with the dot in its middle. */
  gapM: 0.05,
} as const

/**
 * The names of the line and how many more Bots it does not name. A Bot that asks and has failed is named once, as
 * one that asks: its ask is what he can act on.
 */
export function homeNames(bots: readonly (Pick<XrBot, 'slug' | 'label' | 'state' | 'needs'> & Partial<Pick<XrBot, 'cuts'>>)[], most: number = HOME_NAMES.most, order: readonly string[] = []): { names: HomeName[]; more: number } {
  const oldest = (b: Pick<XrBot, 'needs'>) => Math.min(...b.needs.map(n => n.ts))
  // `order`: the askers as they came to the line (D75). A name keeps its place while it asks; a new asker joins after
  // them. Only the askers that are new, and a line laid anew, stand by most asks first.
  const came = (slug: string) => { const i = order.indexOf(slug); return i < 0 ? order.length : i }
  const askers = bots.filter(b => b.needs.length > 0).sort((a, b) => came(a.slug) - came(b.slug) || b.needs.length - a.needs.length || oldest(a) - oldest(b))
  // A Bot whose last ask a board answered here may still be listed as asking until the list is read again: with a
  // cut he has not seen it is named as failed at once (D98 7).
  const failed = bots.filter(b => b.needs.length === 0 && (b.state === 'failed' || (b.state === 'needs' && (b.cuts ?? []).some(c => c.red))))
  const all = [...askers, ...failed].map(b => ({ slug: b.slug, who: b.label, asks: b.needs.length, failed: b.needs.length === 0 }))
  return fitLine(all, most)
}

/** The whole signs of a text: a flag, a joined emoji or a letter with its marks is one, and is never split. */
export function signs(text: string): string[] {
  const Seg = (Intl as unknown as { Segmenter?: new (l?: string, o?: { granularity: 'grapheme' }) => { segment: (t: string) => Iterable<{ segment: string }> } }).Segmenter
  return Seg ? [...new Seg(undefined, { granularity: 'grapheme' }).segment(text)].map(x => x.segment) : [...text]
}

/**
 * The names as the line shows them (D94 5): each whole when it is no wider than `max` (`wide` measures a text),
 * else cut by whole signs and ended with "…". Two names that would then read alike are cut where they differ: the
 * start, "…", and the end ("Resea…Alpha", "Resea…Beta").
 */
export function cutNames(labels: readonly string[], wide: (text: string) => number, max: number): string[] {
  const head = (label: string): string => {
    if (wide(label) <= max) return label
    const s = signs(label)
    while (s.length > 1 && wide(`${s.join('').trimEnd()}…`) > max) s.pop()
    return `${s.join('').trimEnd()}…`
  }
  const apart = (label: string, tail: number): string => {
    const s = signs(label)
    const end = s.slice(-tail).join('').trimStart()
    const start = s.slice(0, Math.max(1, s.length - tail))
    while (start.length > 1 && wide(`${start.join('').trimEnd()}…${end}`) > max) start.pop()
    return `${start.join('').trimEnd()}…${end}`
  }
  const out = labels.map(head)
  const alike = (i: number) => out.some((o, k) => k !== i && o === out[i] && labels[k] !== labels[i])
  labels.forEach((_, i) => {
    if (!alike(i)) return
    const twins = labels.map((_, k) => k).filter(k => out[k] === out[i])
    // The shortest end that tells every twin apart, up to half the name.
    for (let tail = 1; tail <= 12; tail += 1) {
      const tried = twins.map(k => apart(labels[k], Math.min(tail, Math.ceil(signs(labels[k]).length / 2))))
      if (new Set(tried).size === tried.length || tail === 12) { twins.forEach((k, n) => { out[k] = tried[n] }); break }
    }
  })
  return out
}

/**
 * Which name leaves the line when not all fit its width (D94 5): the last asker while more than one asker is named,
 * and only then a failed Bot, the last first. So a failed Bot is never the first to fold.
 */
export function foldNext(names: readonly HomeName[]): number {
  const askers = names.map((n, i) => (n.failed ? -1 : i)).filter(i => i >= 0)
  return askers.length > 1 ? askers[askers.length - 1] : names.length - 1
}

/**
 * Which of `all` (askers first, then the failed) the line names, `most` at the most (D88 2): three askers when three
 * ask, then the Bots that failed, then more askers. So a failed Bot is folded into "+n" only when the places left
 * after three askers are all taken by failed Bots.
 */
export function fitLine(all: readonly HomeName[], most: number = HOME_NAMES.most): { names: HomeName[]; more: number } {
  if (all.length <= most) return { names: [...all], more: 0 }
  const askers = all.filter(n => !n.failed)
  const failed = all.filter(n => n.failed)
  const first = Math.min(askers.length, HOME_NAMES.askersFirst, most)
  const fails = failed.slice(0, most - first)
  const names = [...askers.slice(0, most - fails.length), ...fails]
  return { names, more: all.length - names.length }
}

/**
 * Where each name stands along the strip (metres from its middle): as many of `widths` as fit between `left` and
 * `right` with `gap` between two, and "+n" after them for the rest (`more` beyond the list, plus the names that did
 * not fit); `moreWidth(n)` is how wide "+n" is. Each name's press area is its own words and half the gap to each
 * side, so two never overlap.
 */
export function placeNames(widths: readonly number[], gap: number, moreWidth: (n: number) => number, left: number, right: number, more = 0): { at: { cx: number; w: number; press: number }[]; more: number; moreAt: number } {
  let shown = widths.length
  const need = (n: number) => {
    const rest = more + widths.length - n
    return widths.slice(0, n).reduce((a, w) => a + w, 0) + Math.max(0, n - 1) * gap + (rest > 0 ? (n > 0 ? gap : 0) + moreWidth(rest) : 0)
  }
  while (shown > 0 && need(shown) > right - left) shown -= 1
  const at: { cx: number; w: number; press: number }[] = []
  let x = left
  for (let i = 0; i < shown; i += 1) { at.push({ cx: x + widths[i] / 2, w: widths[i], press: widths[i] + gap }); x += widths[i] + gap }
  return { at, more: more + widths.length - shown, moreAt: x }
}
