/**
 * What stands on a Bot's plot, and what an idle Bot does there. Plain data
 * from the Bot's real fields; the scene turns each item into an object.
 *
 * | Item  | Real field                                  | What it tells           |
 * |-------|---------------------------------------------|-------------------------|
 * | home  | the Bot itself                              | this plot is lived in   |
 * | work  | each thing the Bot is on that is not idle   | its state               |
 * | ask   | each ask that waits on the person           | pinch it to open it     |
 * | goal  | each key result: reading against target     | how far along           |
 * | clock | each scheduled job                          | when it runs next       |
 * | built | each report, and each piece of work at rest | what has been finished  |
 *
 * A thing keeps its place: places are given in a fixed order by a key that
 * does not change (when it was made, or its id), so new things are added
 * after the old ones and nothing shuffles. A plot is never empty: the home
 * is always there, and what was finished stays as built things, so a crew
 * with nothing to do today still has a land that shows what it did.
 *
 * Places are metres from the plot's middle: x to the right, z toward the
 * person, who stands at the near edge. The Bot's own spot is `BOT_SPOT`.
 */
import type { XrGlance } from './glance'
import { PLOT_M } from './land'
import type { XrBot } from './model'
import type { PageKind } from './pages'

export type PlotKind = 'home' | 'work' | 'ask' | 'goal' | 'clock' | 'built'

export interface PlotItem {
  kind: PlotKind
  /** The same thing keeps the same id from one read to the next. */
  id: string
  /** The Bot's field this item shows (see `boundValue` in `sceneDoc.ts`). */
  field: string
  x: number
  z: number
  /** What a pinch on it pulls out, or null. */
  page: { kind: PageKind; slot: string } | null
  /** Things of this kind that have no place of their own. Set on the last one drawn. */
  more: number
}

export const BOT_SPOT: readonly [number, number] = [0, 0.3]

const PLACES: Record<PlotKind, readonly (readonly [number, number])[]> = {
  home: [[0, -1]],
  ask: [[-0.95, 1.15], [0.95, 1.15], [-1.7, 0.75]],
  work: [[-2.1, -0.1], [-2.1, -1.1], [-1.35, -1.75], [-2.4, 0.9]],
  goal: [[2.1, 0.6], [2.1, -0.4], [2.1, -1.4], [2.45, -2.3]],
  clock: [[2.35, 1.7], [1.55, 2.05]],
  built: [[-2.3, -2.55], [-1.5, -2.55], [-0.7, -2.55], [0.1, -2.55], [0.9, -2.55], [1.7, -2.55]],
}

type Draft = Omit<PlotItem, 'x' | 'z' | 'more' | 'kind'>

function place(kind: PlotKind, drafts: Draft[]): PlotItem[] {
  const places = PLACES[kind]
  return drafts.slice(0, places.length).map((d, i, shown) => ({
    ...d, kind, x: places[i][0], z: places[i][1], more: i === shown.length - 1 ? drafts.length - shown.length : 0,
  }))
}

const byId = (a: { id: string }, b: { id: string }): number => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)

/** The key a report is bound by: when it was written, and its title. */
export const reportKey = (r: { at: number; title: string }): string => `report:${r.at}:${r.title}`

/** Everything that stands on one Bot's plot. `glance` may still be loading: the plot is then drawn from the Bot alone. */
export function plotPlan(bot: XrBot, glance: XrGlance | null): PlotItem[] {
  const chat = (slot: string) => ({ kind: 'chat' as const, slot })
  const dash = { kind: 'dash' as const, slot: bot.thread }
  const home: Draft[] = [{ id: 'home', field: 'state', page: chat(bot.thread) }]
  const work: Draft[] = bot.things.filter(t => t.state !== 'idle').map(t => ({ id: `thing:${t.slot}`, field: `thing:${t.slot}`, page: chat(t.slot) })).sort(byId)
  const asks: Draft[] = [...bot.needs].sort((a, b) => a.ts - b.ts || byId(a, b)).map(n => ({ id: `need:${n.id}`, field: `need:${n.id}`, page: dash }))
  const goals: Draft[] = (glance?.numbers ?? []).map((_, i) => ({ id: `goal:${i}`, field: `goal:${i}`, page: dash }))
  const clocks: Draft[] = bot.jobs.map(j => ({ id: `job:${j.id}`, field: `job:${j.id}`, page: null }))
  // What was finished, oldest first, so a new one is added at the end and the old ones stay where they are.
  const built: Draft[] = [
    ...(glance?.reports ?? []).map(r => ({ id: reportKey(r), at: r.at, page: dash as Draft['page'] })),
    ...bot.things.filter(t => t.state === 'idle' && !t.direct).map(t => ({ id: `thing:${t.slot}`, at: t.ts, page: chat(t.slot) as Draft['page'] })),
  ].sort((a, b) => a.at - b.at || byId(a, b)).map(d => ({ id: d.id, field: d.id, page: d.page }))
  return [...place('home', home), ...place('ask', asks), ...place('work', work), ...place('goal', goals), ...place('clock', clocks), ...place('built', built)]
}

export type IdleAct = 'tend' | 'sit' | 'look' | 'nap'
export interface IdleStep { act: IdleAct; x: number; z: number }

/** A small, stable number from a string (FNV-1a). */
function hash(s: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i += 1) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) }
  return h >>> 0
}

const ACTS: readonly IdleAct[] = ['tend', 'look', 'sit', 'tend', 'nap', 'look']
const EDGE = PLOT_M / 2 - 0.4

/** Which of its picks a Bot is on at a moment: `idleLife` picks anew when this number changes. */
export function idleStep(slug: string, seconds: number): number {
  const h = hash(slug)
  return Math.floor((seconds + (h % 97)) / (9 + (h % 7)))
}

/**
 * What an idle Bot does at a moment: it tends something it built, looks
 * around, sits, or naps. It changes every 9 to 15 seconds, each Bot on its
 * own beat, and it never leaves its plot. The same Bot at the same second
 * does the same thing, so the land is not different on every screen.
 */
export function idleLife(slug: string, items: readonly { kind: string; x: number; z: number }[], seconds: number): IdleStep {
  const step = idleStep(slug, seconds)
  const roll = hash(`${slug}:${step}`)
  const act = ACTS[roll % ACTS.length]
  const clampIn = (v: number) => Math.max(-EDGE, Math.min(EDGE, v))
  if (act === 'tend') {
    const own = items.filter(i => i.kind === 'built' || i.kind === 'goal' || i.kind === 'home')
    if (!own.length) return { act: 'sit', x: BOT_SPOT[0], z: BOT_SPOT[1] }
    const it = own[(roll >>> 8) % own.length]
    // It stands a step short of the thing, on the side nearer the middle.
    const d = Math.hypot(it.x, it.z) || 1
    return { act, x: clampIn(it.x - (it.x / d) * 0.55), z: clampIn(it.z - (it.z / d) * 0.55) }
  }
  if (act === 'look') {
    const a = ((roll >>> 8) % 360) * (Math.PI / 180)
    return { act, x: clampIn(BOT_SPOT[0] + Math.cos(a) * 0.9), z: clampIn(BOT_SPOT[1] + Math.sin(a) * 0.6) }
  }
  if (act === 'nap') return { act, x: 0.55, z: -0.45 }
  return { act, x: BOT_SPOT[0], z: BOT_SPOT[1] }
}
