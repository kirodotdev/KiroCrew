/** Recall lights its origin cluster once; every mark shares the strike and fade. */
export const RISE_MS = 80
export const SPREAD_MS = 200
export const STRIKE_MS = 320
export const FADE_MS = 3000
export const TOTAL_MS = SPREAD_MS + STRIKE_MS + FADE_MS

/** A normalized exponential tail: three seconds, with a 1.2 second time constant. */
export function afterglow(progress: number): number {
  const p = Math.min(1, Math.max(0, progress))
  return (Math.exp(-2.5 * p) - Math.exp(-2.5)) / (1 - Math.exp(-2.5))
}

/** Ease-out-back strike, two weaker brightness re-strikes, then a long afterglow. */
export function flashAt(age: number): number {
  if (age < 0) return 0
  if (age < 60) { const p = age / 60 - 1; return 1 + 2.70158 * p ** 3 + 1.70158 * p ** 2 }
  if (age < 100) return 1 - 0.78 * (age - 60) / 40
  if (age < 120) return 0.22
  if (age < 140) return 0.22 + 0.5 * (age - 120) / 20
  if (age < 180) return 0.72 - 0.54 * (age - 140) / 40
  if (age < 220) return 0.18
  if (age < 240) return 0.18 + 0.32 * (age - 220) / 20
  if (age < STRIKE_MS) return 0.5 - 0.25 * (age - 240) / 80
  return 0.25 * afterglow((age - STRIKE_MS) / FADE_MS)
}

export type Light = { at: number; hold: number; s: number; instant?: boolean }
export type Spread = { lights: Map<number, Light>; end: number }
export const lightEnd = (l: Light) => l.at + (l.instant ? 0 : RISE_MS) + l.hold + FADE_MS
export function lightAt(l: Light, now: number): number {
  const age = now - l.at
  if (age < 0) return 0
  const riseMs = l.instant ? 0 : RISE_MS
  const rise = l.instant ? 1 : Math.min(1, age / riseMs)
  return l.s * rise * rise * (3 - 2 * rise) * afterglow((age - riseMs - l.hold) / FADE_MS)
}
export function byCluster(items: readonly { cluster: number }[]): Map<number, number[]> {
  const out = new Map<number, number[]>()
  items.forEach((p, i) => { const m = out.get(p.cluster); if (m) m.push(i); else out.set(p.cluster, [i]) })
  return out
}
export type SpreadInput = {
  hit: number; now: number
  items: readonly { x: number; y: number; cluster: number }[]
  members: Map<number, number[]>
}
export function spread({ hit, now, items, members }: SpreadInput): Spread {
  const origin = items[hit], indices = members.get(origin.cluster) ?? [hit]
  const lights = new Map<number, Light>()
  let radius = 0
  for (const i of indices) radius = Math.max(radius, Math.hypot(items[i].x - origin.x, items[i].y - origin.y))
  // A compact seed stays point-like even on the dense overview.
  const seeds = new Set(indices.filter(i => Math.hypot(items[i].x - origin.x, items[i].y - origin.y) <= radius * 0.14)
    .sort((a, b) => Math.hypot(items[a].x - origin.x, items[a].y - origin.y) - Math.hypot(items[b].x - origin.x, items[b].y - origin.y)).slice(0, 5))
  for (const i of indices) {
    const p = items[i], distance = radius ? Math.hypot(p.x - origin.x, p.y - origin.y) / radius : 0
    const instant = seeds.has(i)
    const delay = instant ? 0 : Math.round(distance * (SPREAD_MS - RISE_MS))
    lights.set(i, { at: now + delay, hold: SPREAD_MS + STRIKE_MS - delay - (instant ? 0 : RISE_MS), s: 0.999, instant })
  }
  return { lights, end: now + TOTAL_MS }
}
/** Session starts light their own marks without spreading to the cluster. */
export function flash(hit: number, now: number): Spread {
  const l = { at: now, hold: STRIKE_MS, s: 0.999 }
  return { lights: new Map([[hit, l]]), end: lightEnd(l) }
}
export class HitQueue {
  private waiting: { id: string; kind: string; cluster?: number }[] = []
  private tokens: number
  private last: number | null = null
  constructor(readonly rate = 20, readonly cap = 20) { this.tokens = rate }
  private refill(now: number) {
    if (this.last !== null) this.tokens = Math.min(this.rate, this.tokens + ((now - this.last) / 1000) * this.rate)
    this.last = now
  }
  push(ids: readonly string[], kind: string, now: number, clusters?: Record<string, number>) {
    this.refill(now)
    for (const id of ids) {
      if (this.waiting.some(w => w.id === id)) continue
      const cluster = clusters?.[id]
      this.waiting.push({ id, kind, cluster: Number.isInteger(cluster) ? cluster : undefined })
      if (this.waiting.length > this.cap) this.waiting.shift()
    }
  }
  take(now: number): { id: string; kind: string; cluster?: number }[] {
    this.refill(now)
    const n = Math.min(Math.floor(this.tokens), this.waiting.length)
    this.tokens -= n
    return this.waiting.splice(0, n)
  }
  get size() { return this.waiting.length }
  clear() { this.waiting = [] }
}
