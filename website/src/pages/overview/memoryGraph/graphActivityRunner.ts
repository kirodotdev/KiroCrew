/** Mark activation lives entirely in the point shader, without another canvas. */
import type { Graph as CosmosGraph } from '@cosmos.gl/graph'
import type { GraphBbox, GraphCluster, GraphNodeRow } from '../../../api/client/memory'
import { HitQueue, byCluster, flash, lightEnd, spread, RISE_MS, FADE_MS, type Light } from './graphActivity'
export const BREATH_MS = 6000
export const BREATH_AMP = 0.035
export const BREATH_SPREAD = Math.PI
export const BREATH_FPS = 20
export const LIT_GROWTH = 0.12
export const LINK_BOW = 0.15
export const MEMORY_ACTIVITY_EVENT = 'mc:memory-activity'
export type ActivityUniforms = { clock: number[]; centre: number[]; light: number[] }
export type ActivityFrame = { store: string; kind: string; ids: string[]; ts: number; layout?: string; clusters?: Record<string, number> }
export type RunnerDeps = {
  graph: () => CosmosGraph | null
  shown: () => { mode: 'clusters' | 'nodes'; items: readonly (GraphCluster | GraphNodeRow)[] }
  layout: () => string | undefined
  bounds: () => GraphBbox | undefined
  dark: () => boolean; movedAt: () => number; now?: () => number
  /** The breath drawn as one image (graphBreathLayer); null breathes in the point shader. */
  layer?: () => { draw(): void } | null
}
export class ActivityRunner {
  readonly uniforms: ActivityUniforms = { clock: [0, 0, 0, 0], centre: [0, 0, LIT_GROWTH, 0], light: [0, 0, 0, 0] }
  private readonly queue = new HitQueue()
  private lights = new Map<number, Light>()
  private end = 0
  private frame: number | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private lastDraw = -Infinity
  private on = false
  private uploaded = false
  private items: readonly unknown[] | null = null
  private index = new Map<string, number>()
  private members = new Map<number, number[]>()
  private readonly base: number
  private readonly now: () => number
  constructor(private readonly deps: RunnerDeps) {
    this.now = deps.now ?? (() => performance.now())
    // shortcut: float32 milliseconds lose precision after 4.6 hours; rebase
    // while idle if long-lived tabs need sub-millisecond activation clocks.
    this.base = this.now()
  }
  setRunning(on: boolean) {
    if (on === this.on) return
    this.on = on
    this.shaderBreath()
    this.uniforms.light[0] = RISE_MS
    this.uniforms.light[2] = on ? FADE_MS : 0
    if (on) { this.schedule(); return }
    this.cancel()
    this.queue.clear(); this.clearLights()
    const g = this.deps.graph(); if (g) draw(g)
  }
  get running() { return this.on }
  /** The breath's amplitude for the breath layer: zero while stopped. */
  get breathAmp() { return this.on ? BREATH_AMP : 0 }
  /** The point shader breathes only when no breath layer draws the wave. */
  private shaderBreath() { this.uniforms.clock[2] = this.on && !this.deps.layer?.() ? BREATH_AMP : 0 }
  private cancel() {
    if (this.frame !== null) cancelAnimationFrame(this.frame)
    if (this.timer !== null) clearTimeout(this.timer)
    this.frame = null; this.timer = null
  }
  push(frame: ActivityFrame) {
    if (!this.on || !Array.isArray(frame.ids)) return
    const clusters = frame.layout && frame.layout === this.deps.layout() ? frame.clusters : undefined
    this.queue.push(frame.ids.filter(i => typeof i === 'string'), frame.kind, this.now(), clusters)
    this.schedule()
  }
  reset() { this.items = null; this.queue.clear(); this.clearLights() }
  /** A camera pick changes indices, not the retained memories' activation clocks. */
  remap(before: readonly GraphNodeRow[]) {
    const shown = this.indexed(), lights = new Map<number, Light>()
    for (const [i, light] of this.lights) {
      const next = this.index.get(before[i].id)
      if (next !== undefined) lights.set(next, light)
    }
    this.lights = lights
    if (!lights.size) this.end = 0
    if (this.uploaded) this.upload(shown.items.length)
  }
  dispose() { this.cancel(); this.on = false }
  private clearLights() {
    this.lights.clear(); this.end = 0
    if (this.uploaded) this.upload(this.deps.shown().items.length)
  }
  /** The next step: on the next animation frame, or after `wait` ms when only the breath is left. */
  private schedule(wait = 0) {
    if (!this.on || this.frame !== null) return
    if (wait > 0) { this.timer ??= setTimeout(() => { this.timer = null; this.schedule() }, wait); return }
    if (this.timer !== null) { clearTimeout(this.timer); this.timer = null }
    this.frame = requestAnimationFrame(() => { this.frame = null; this.tick() })
  }
  private indexed() {
    const shown = this.deps.shown()
    if (this.items !== shown.items) {
      this.items = shown.items; this.index = new Map()
      if (shown.mode === 'nodes') {
        shown.items.forEach((n, i) => this.index.set((n as GraphNodeRow).id, i))
        this.members = byCluster(shown.items as GraphNodeRow[])
      } else this.members = new Map()
    }
    return shown
  }
  private lightHits(now: number) {
    const hits = this.queue.take(now)
    if (!hits.length) return false
    const shown = this.indexed()
    for (const [i, l] of this.lights) if (lightEnd(l) <= now) this.lights.delete(i)
    const litClusters = new Set<number>()
    for (const { id, kind, cluster: hitCluster } of hits) {
      if (shown.mode === 'clusters') {
        const disc = shown.items.findIndex(c => (c as GraphCluster).id === hitCluster)
        if (disc >= 0) {
          const s = flash(disc, now)
          for (const [i, l] of s.lights) this.lights.set(i, l)
          this.end = Math.max(this.end, s.end)
        }
        continue
      }
      const hit = this.index.get(id)
      if (hit === undefined) continue
      const cluster = (shown.items[hit] as GraphNodeRow).cluster
      // One origin per cluster per batch, even if recall returns many matches.
      if (kind === 'recall' && litClusters.has(cluster)) continue
      if (kind === 'recall') litClusters.add(cluster)
      const s = kind === 'recall' ? spread({ hit, now, items: shown.items as GraphNodeRow[], members: this.members }) : flash(hit, now)
      for (const [i, l] of s.lights) this.lights.set(i, l)
      this.end = Math.max(this.end, s.end)
    }
    this.upload(shown.items.length)
    return true
  }
  /** Upload distance-derived arrivals and holds once; all holds end together. */
  private upload(n: number) {
    const g = this.deps.graph(); if (!g) return
    const at = new Float32Array(n).fill(-1), hold = new Float32Array(n)
    for (const [i, l] of this.lights) {
      if (i >= n) continue
      at[i] = Math.max(0, l.at - this.base)
      hold[i] = (l.hold + l.s) * (l.instant ? -1 : 1)
    }
    const upload = (g as unknown as { setPointActivity?: (at: Float32Array, strength: Float32Array) => void }).setPointActivity
    if (upload) upload.call(g, at, hold)
    else { g.setPointImageIndices(at); g.setPointImageSizes(hold); g.render(0, 0) }
    this.uploaded = this.lights.size > 0
  }
  private tick() {
    if (!this.on) return
    const now = this.now(), t = now - this.base, lit = this.lightHits(now), bounds = this.deps.bounds()
    if (bounds) {
      this.uniforms.centre[0] = (bounds[0] + bounds[2]) / 2; this.uniforms.centre[1] = (bounds[1] + bounds[3]) / 2
      this.uniforms.clock[3] = BREATH_SPREAD / Math.max(1e-9, Math.max(bounds[2] - bounds[0], bounds[3] - bounds[1]) / 2)
    }
    this.uniforms.light[3] = this.deps.dark() ? 1 : 0
    this.uniforms.clock[0] = t; this.uniforms.clock[1] = (2 * Math.PI * t) / BREATH_MS
    this.shaderBreath()
    const active = lit || now < this.end, g = this.deps.graph(), layer = this.deps.layer?.()
    if (g && now - this.deps.movedAt() >= 150) {
      // Lit marks change every frame: cosmos.gl draws them (and the layer copies that frame).
      if (active || this.lights.size) { this.lastDraw = now; draw(g) }
      else if (now - this.lastDraw >= 1000 / BREATH_FPS) { this.lastDraw = now; if (layer) layer.draw(); else draw(g) }
    }
    if (!active) this.lights.clear()
    // Only the breath left: no animation frame until its next step.
    this.schedule(active || this.queue.size ? 0 : Math.max(1, this.lastDraw + 1000 / BREATH_FPS - now))
  }
}
/** Queue a draw without forcing cosmos.gl to collect GPU picking synchronously. */
function draw(g: CosmosGraph) {
  const own = (g as unknown as { requestRender?: () => void }).requestRender
  if (own) own.call(g); else g.renderOneFrame()
}
