/**
 * The laundry-folding game drawn on a canvas while the dashboard waits for its
 * gateway (`./WaitGame.tsx`). Drag clothes from the basket to the table, tap to
 * fold, stack them on the pile. Now and then the folded sheet at the bottom of
 * the pile turns into the Kiro ghost and floats away.
 *
 * Imperative on purpose: the canvas loop owns every bit of game state, and React
 * only mounts the markup and calls `startLaundry` / `stop`. Framer Motion
 * cannot drive a canvas; the in-app precedent for a canvas scene is
 * `pages/scenes/mission-control/MissionControlScene.tsx`.
 *
 * It makes no request and touches no storage. Every string comes in through
 * `opts.text()`, read at the moment it is shown, so a language switch reaches
 * the next message. Colours come from the root's `--paper` / `--ink` CSS
 * properties, which the stylesheet maps onto the dashboard's theme tokens.
 */
import {
  type Box, type Fold, type Layer, type Pt, type Rand,
  PI, align, boxOf, clamp, creaseExtent, densify, easeBack, easeIn, easeInOut, easeOut,
  foldLayers, ghostShape, lerp, resample, shuffle, NG,
} from './geometry'

/** Every word the game shows or speaks. Item names are left out on purpose:
 *  one short line per move, never a sentence assembled from parts. */
export interface LaundryText {
  /** The count in the reader's numerals, for the visible counter (no grouping). */
  folded: (n: number) => string
  item_basket: string
  item_table: string
  item_part_folded: string
  item_folded: string
  placed_table: string
  placed_basket: string
  placed_pile: string
  fold_done: string
  fold_step: string
  fold_first: string
  table_full: string
  picked_up: string
  put_back: string
  over_basket: string
  over_table: string
  over_pile: string
  /** The word drawn under the pile, so a person knows what "drag to the pile" means. */
  pile: string
  ghost: string
  pile_full: string
  started_over: string
}

export interface LaundryOptions {
  text: () => LaundryText
  /** Random source; tests pass a seeded one. */
  random?: Rand
  /** Force the ghost on every eligible stack (tests and screenshots). */
  forceGhost?: boolean
}

export interface LaundryHandle {
  restart: () => void
  stop: () => void
  /** Re-read every label the engine wrote (counter, item buttons, the drawn Pile word): call after a language change. */
  retext: () => void
}

type ItemType = 'shirt' | 'towel' | 'sock' | 'sheet' | 'trousers'
type Zone = 'basket' | 'table' | 'pile' | 'held' | 'drag' | 'leaving' | 'egg' | 'gone'
interface TypeDef { ck: number; poly: Pt[]; lines: Pt[][]; folds: Fold[] }
interface Cloth { A: Pt[]; B: Pt[]; C: Pt[]; nrm: Pt[]; ny: number[]; eyes: number[][]; gc: Pt; t0: number }
interface GhostFly {
  base: Pt[]; mir: Pt[]; eyes: number[][]; top: number; h: number; t0: number
  path: { pts: Pt[]; seg: number[]; len: number }; look: Pt; blink: number
  ox: number; oy: number; ovx: number; ovy: number; vx: number; vy: number
  face: number; faceTo: number; last: number; wph: number
}
interface Item {
  id: number; type: ItemType; T: TypeDef; nf: number; step: number; queued: number
  fold: { f: Fold; t0: number; ext: [number, number] } | null
  layers: Layer[]; crumple: number; x: number; y: number; rot: number; alpha: number
  zone: Zone; slot: number; sdy: number; u: number; ph: number; z: number; hx: number; hy: number
  wb: Box | null; btn: HTMLButtonElement | null; cloth: Cloth | null; ghost: GhostFly | null
  cpts: Pt[]; clines: Pt[][]; cbox: Box; wr: Pt[][]
  hp: Pt[][]; tf: { x: number; y: number; r: number }
}
interface Hold { it: Item; from: { zone: Zone; u: number; slot: number }; zone: Zone }
interface Drag { it: Item; id: number; sx: number; sy: number; dx: number; dy: number; moved: boolean; zone: Zone; from: { zone: Zone; u: number; slot: number } }

// After the first ghost, each stack rolls this chance while a sheet is at the bottom of the pile. Every pile starts on
// a folded sheet and holds about six stacks, so 0.2 gives about one ghost per eight stacks (1 - 0.8^6 = 0.74 per pile).
export const EGG_CHANCE = 0.2
const FOLD_MS = 480

// Flat outlines in local units (y down). Folds run in order; horizontal folds always turn the top down, so the
// bottom edge stays on the table.
const TYPES: Record<ItemType, TypeDef> = {
  shirt: { ck: 0.62,
    poly: [[-13, -45], [-8, -39], [0, -36], [8, -39], [13, -45], [30, -45], [58, -28], [50, -14], [40, -21], [40, 45], [-40, 45], [-40, -21], [-50, -14], [-58, -28], [-30, -45]],
    lines: [[[-36, 39], [36, 39]], [[-10, -43], [0, -33], [10, -43]]],
    folds: [{ ax: 0, at: -25, dir: -1 }, { ax: 0, at: 25, dir: 1 }, { ax: 1, at: 0, dir: -1 }] },
  towel: { ck: 0.56,
    poly: [[-60, -35], [60, -35], [60, 35], [-60, 35]],
    lines: [[[-48, -33], [-48, 33]], [[-43, -33], [-43, 33]], [[43, -33], [43, 33]], [[48, -33], [48, 33]]],
    folds: [{ ax: 0, at: 20, dir: 1 }, { ax: 0, at: -20, dir: -1 }, { ax: 1, at: 0, dir: -1 }] },
  sock: { ck: 0.8,
    poly: [[-15, -40], [15, -40], [15, 14], [30, 16], [39, 21], [43, 28], [40, 35], [32, 40], [-9, 40], [-14, 36], [-15, 30]],
    lines: [[[-14, -31], [14, -31]], [[33, 19], [36, 28], [32, 38]]],
    folds: [{ ax: 0, at: 15, dir: 1 }, { ax: 1, at: 0, dir: -1 }] },
  sheet: { ck: 0.42,
    poly: [[-105, -60], [105, -60], [105, 60], [-105, 60]],
    lines: [[[-103, -51], [103, -51]]],
    folds: [{ ax: 0, at: 35, dir: 1 }, { ax: 0, at: -35, dir: -1 }, { ax: 1, at: 0, dir: -1 }] },
  trousers: { ck: 0.56,
    poly: [[-30, -65], [30, -65], [38, 65], [6, 65], [0, -12], [-6, 65], [-38, 65]],
    lines: [[[-30, -56], [30, -56]], [[0, -56], [0, -36]], [[-24, -56], [-17, -44]], [[24, -56], [17, -44]]],
    folds: [{ ax: 0, at: 0, dir: 1 }, { ax: 1, at: 0, dir: -1 }, { ax: 1, at: 32, dir: -1 }] },
}
const ORDER: ItemType[] = ['shirt', 'towel', 'sock', 'sheet', 'trousers']

// Crease lines drawn on the neat folded rectangle, per type.
function finalLines(type: ItemType, b: Box): Pt[][] {
  const X = (t: number) => lerp(b.x0, b.x1, t), Y = (t: number) => lerp(b.y0, b.y1, t)
  const h = (y: number, a = 0.08, z = 0.92): Pt[] => [[X(a), Y(y)], [X(z), Y(y)]]
  const v = (x: number, a: number, z: number): Pt[] => [[X(x), Y(a)], [X(x), Y(z)]]
  switch (type) {
    case 'shirt': return [[[X(0.34), Y(0)], [X(0.42), Y(0.15)], [X(0.5), Y(0.19)], [X(0.58), Y(0.15)], [X(0.66), Y(0)]], h(0.62)]
    case 'towel': return [h(0.66, 0.06, 0.94), h(0.78, 0.06, 0.94)]
    case 'sock': return [h(0.24), v(0.5, 0.42, 0.88)]
    case 'sheet': return [h(0.5, 0.06, 0.94), v(0.5, 0.1, 0.9)]
    default: return [h(0.22), v(0.62, 0.32, 0.9)]
  }
}

// Scene layouts in design units, scaled to fit: landscape and portrait.
interface Layout { w: number; h: number; floor: number; top: number; tx0: number; tx1: number; bx: number; bw: number; bh: number; px: number; fx0: number; fx1: number; pmax: number; rimY: number; hud: number }
/** The portrait scene stacks the basket under the table. On a phone-width pane that reads as a mistake, and the wide scene
 *  (basket beside the table) is what the card shows, so portrait wins only when it draws at least this much larger. */
const PORTRAIT_BIAS = 1.6
const LAND: Layout = { w: 900, h: 500, floor: 440, top: 300, tx0: 330, tx1: 880, bx: 165, bw: 230, bh: 108, px: 800, fx0: 345, fx1: 745, pmax: 230, rimY: 332, hud: 0 }
const PORT: Layout = { w: 400, h: 720, floor: 620, top: 390, tx0: 12, tx1: 388, bx: 150, bw: 200, bh: 96, px: 326, fx0: 18, fx1: 278, pmax: 270, rimY: 524, hud: 0 }

/**
 * Start the game inside `root`, which must already hold the markup
 * `WaitGame` renders (a canvas and the `.laundry-keys`, `-count`, `-hint`, `-toast`, `-caption` and `-live` elements).
 * Returns `stop()`, which cancels the frame loop and removes every listener and observer it added.
 */
export interface TweenRec { o: Record<string, number>; from: Record<string, number>; to: Record<string, number>; t0: number; dur: number; ease: (t: number) => number; done?: () => void }

/** One frame of tweens: every tween steps, and the callbacks of those that finished run afterwards, in order. A callback
 *  that starts a tween cancels others on its keys, which splices the list: run inside the loop, that would skip a tween. */
export function runTweenList(list: TweenRec[], now: number) {
  const finished: (() => void)[] = []
  for (let i = 0; i < list.length; i++) {
    const tw = list[i]
    const p = tw.dur > 0 ? clamp((now - tw.t0) / tw.dur, 0, 1) : 1
    const e = tw.ease(p)
    for (const k in tw.to) tw.o[k] = tw.from[k] + (tw.to[k] - tw.from[k]) * e
    if (p >= 1) { list.splice(i--, 1); if (tw.done) finished.push(tw.done) }
  }
  for (const done of finished) done()
}

export function startLaundry(root: HTMLElement, opts: LaundryOptions): LaundryHandle {
  const rand = opts.random || Math.random
  const rnd = (a: number, b: number) => a + rand() * (b - a)
  const T = opts.text
  const q = <E extends Element>(sel: string) => root.querySelector(sel) as E
  const cv = q<HTMLCanvasElement>('canvas')
  const ctx = cv.getContext('2d') as CanvasRenderingContext2D
  if (!ctx) return { restart: () => {}, stop: () => {}, retext: () => {} } // no 2D canvas in this browser: show nothing, break nothing
  const keysEl = q<HTMLElement>('.laundry-keys'), countEl = q<HTMLElement>('.laundry-count'), hudEl = q<HTMLElement>('.laundry-hud'), toastEl = q<HTMLElement>('.laundry-toast')
  const liveEl = q<HTMLElement>('.laundry-live'), hintEl = q<HTMLElement>('.laundry-hint'), captionEl = q<HTMLElement>('.laundry-caption')
  const mqRM = window.matchMedia('(prefers-reduced-motion: reduce)')
  let RM = mqRM.matches
  let stopped = false
  const cleanups: (() => void)[] = []
  const on = (el: Window | HTMLElement, type: string, fn: (e: never) => void, capture = false) => {
    const opts = capture ? { capture: true } : undefined
    el.addEventListener(type, fn as EventListener, opts)
    cleanups.push(() => el.removeEventListener(type, fn as EventListener, opts))
  }

  let ink = '#3b3b3b', paper = '#fbfbf9'
  function readTheme() {
    const cs = getComputedStyle(root)
    ink = cs.getPropertyValue('--ink').trim() || ink
    paper = cs.getPropertyValue('--paper').trim() || paper
  }

  // ---------- frame scheduling ----------
  // A frame is requested only while something needs one: under reduced motion an idle scene stops the loop entirely.
  let raf = 0
  let now = performance.now()
  let dirty = true
  function kick() { if (!raf && !stopped) raf = window.requestAnimationFrame(frame) }
  function markDirty() { dirty = true; kick() }

  const tweens: TweenRec[] = []
  const timers: { at: number; fn: () => void }[] = []
  function later(ms: number, fn: () => void) { timers.push({ at: performance.now() + ms, fn }); kick() }
  function runTimers() {
    for (let i = 0; i < timers.length; i++) {
      if (timers[i].at <= now) { const t = timers.splice(i--, 1)[0]; t.fn() }
    }
  }
  // keep=true still animates under reduced motion (fades only).
  function tween(it: Item, to: Partial<Record<'x' | 'y' | 'rot' | 'crumple' | 'alpha', number>>, dur: number, ease?: (t: number) => number, done?: () => void, keep?: boolean) {
    const o = it as unknown as Record<string, number>
    cancelKeys(it, Object.keys(to))
    const from: Record<string, number> = {}
    for (const k in to) from[k] = o[k]
    tweens.push({ o, from, to: { ...to } as Record<string, number>, t0: performance.now(), dur: RM && !keep ? 0 : dur, ease: ease || easeOut, done })
    kick()
  }
  function cancelKeys(it: Item, keys: string[]) {
    const o = it as unknown as Record<string, number>
    for (let i = tweens.length - 1; i >= 0; i--) {
      const tw = tweens[i]
      if (tw.o !== o) continue
      for (const k of keys) delete tw.to[k]
      if (!Object.keys(tw.to).length) tweens.splice(i, 1)
    }
  }
  function runTweens() { runTweenList(tweens, now) }

  // ---------- items ----------
  // A crumpled version of the flat outline: smaller, lumpy and slightly twisted. The same transform applies to the
  // detail lines (stripes, cuffs, collar), so each item keeps a cue to what it is.
  function crumpler(pts: Pt[], scale: number) {
    const b = boxOf([pts]), cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2
    const p1 = rnd(0, 6.3), p2 = rnd(0, 6.3), p3 = rnd(0, 6.3), p4 = rnd(0, 6.3)
    return (p: Pt, j = 0): Pt => {
      const dx = p[0] - cx, dy = p[1] - cy, a = Math.atan2(dy, dx)
      const k = scale * (1 + 0.1 * Math.sin(2 * a + p1) + 0.07 * Math.sin(3 * a + p2) + 0.035 * Math.sin(7 * a + p4) + j)
      const tw = 0.1 * Math.sin(a + p3), c = Math.cos(tw), s = Math.sin(tw)
      return [cx + (dx * c - dy * s) * k, cy + (dx * s + dy * c) * k]
    }
  }
  function wrinkles(cpts: Pt[]): Pt[][] {
    const b = boxOf([cpts]), w = b.x1 - b.x0, h = b.y1 - b.y0, out: Pt[][] = []
    for (let i = 0; i < 3; i++) {
      const cx = b.x0 + w * rnd(0.3, 0.7), cy = b.y0 + h * rnd(0.3, 0.7), a = rnd(0, PI), L = Math.min(w, h) * rnd(0.3, 0.45)
      const ux = (Math.cos(a) * L) / 2, uy = (Math.sin(a) * L) / 2, bend = rnd(-0.3, 0.3)
      out.push([[cx - ux, cy - uy], [cx - uy * bend * 2, cy + ux * bend * 2], [cx + ux, cy + uy]])
    }
    return out
  }
  let uid = 0
  function makeItem(type: ItemType): Item {
    const Td = TYPES[type]
    const pts = densify(Td.poly, 6, true, 0.6, rand)
    const layers = [{ pts, lines: Td.lines.map(l => densify(l, 6, false, 0.4, rand)) }]
    const cf = crumpler(pts, Td.ck)
    const cpts = pts.map(p => cf(p, rnd(-0.012, 0.012)))
    return { id: ++uid, type, T: Td, nf: Td.folds.length, step: 0, queued: 0, fold: null, layers,
      crumple: 1, x: 0, y: 0, rot: 0, alpha: 1, zone: 'basket', slot: 0, sdy: 0, u: 0.5, z: 0, hx: 0, hy: 0,
      ph: rnd(0, 6.28), wb: null, btn: null, cloth: null, ghost: null,
      cpts, clines: layers[0].lines.map(l => l.map(p => cf(p))), cbox: boxOf([cpts]), wr: wrinkles(cpts).slice(0, 2),
      hp: [], tf: { x: 0, y: 0, r: 0 } }
  }

  // ---------- scene layout ----------
  // Each game measures into its own copies, so the counter height it writes is never shared.
  const land: Layout = { ...LAND }, port: Layout = { ...PORT }
  let G = land, s = 1, ox = 0, oy = 0, W = 0, H = 0, LW = 1.5
  const V = { x0: 0, y0: 0, x1: 900, y1: 500 }
  const S = { items: [] as Item[], pile: [] as Item[], count: 0, drag: null as Drag | null, hold: null as Hold | null,
    settling: false, started: false, ztop: 10, incoming: 0, ghosts: 0, firstGhost: rand() < 0.5 ? 1 : 2 }
  const DASH: { x: number; y: number; l: number }[] = []
  for (let x = -1600; x < 2600; x += rnd(26, 64)) DASH.push({ x, y: rnd(6, 16), l: rnd(3, 12) })

  function layout() {
    const r = root.getBoundingClientRect()
    W = Math.max(1, r.width); H = Math.max(1, r.height)
    // keep the scene above the caption so short embeds do not draw the floor under the text
    // and below the counter and button, which wrap onto more rows in a narrow game
    const capH = Math.min(H * 0.25, (captionEl.offsetHeight || 17) + 14), hudH = Math.min(H * 0.3, hudEl.offsetHeight + 12), SH = Math.max(1, H - capH - hudH)
    const sl = Math.min(W / LAND.w, SH / LAND.h), sp = Math.min(W / PORT.w, SH / PORT.h)
    const g = sp > sl * PORTRAIT_BIAS ? port : land, changed = g !== G
    G = g; G.rimY = G.floor - G.bh
    s = g === port ? sp : sl
    root.dataset.layout = g === port ? 'portrait' : 'landscape'
    // anchored just under the counter row: a narrow, tall pane would otherwise leave a band of nothing between them
    ox = (W - G.w * s) / 2; oy = hudH
    V.x0 = -ox / s; V.y0 = -oy / s; V.x1 = (W - ox) / s; V.y1 = (H - oy) / s
    const dpr = window.devicePixelRatio || 1
    watchDpr(dpr)
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr)
    LW = clamp(1.5 * s, 1.25, 2.6) / s
    { const cb = hudEl.getBoundingClientRect(); G.hud = (cb.bottom - r.top - oy) / s } // the counter row's lowest edge, in scene units
    pileLayout()
    for (const it of S.items) {
      if (it.zone !== 'basket' && it.zone !== 'table' && it.zone !== 'pile') continue
      cancelKeys(it, ['x', 'y'])
      const h = home(it)
      it.x = h.x; it.y = h.y
    }
    if (S.hold) kbShow()
    if (changed) for (const it of S.items) if (it.zone === 'leaving') it.alpha = 0
    markDirty()
  }
  // A move to a monitor with another scale changes the pixel ratio with no resize, so the ResizeObserver never fires: a
  // media query for the ratio the canvas was allocated at lays out again when it stops matching, frames running or not.
  let dprQuery: MediaQueryList | null = null
  function watchDpr(dpr: number) {
    dprQuery?.removeEventListener('change', layout)
    dprQuery = typeof window.matchMedia === 'function' ? window.matchMedia(`(resolution: ${dpr}dppx)`) : null
    dprQuery?.addEventListener('change', layout)
  }
  cleanups.push(() => { dprQuery?.removeEventListener('change', layout); dprQuery = null })
  function localBox(it: Item) { return boxOf(it.layers.map(l => l.pts)) }
  function basketHome(it: Item) {
    const c = it.cbox
    return { x: G.bx + (it.slot - 2) * G.bw * 0.17 - (c.x0 + c.x1) / 2, y: G.rimY - 12 + it.sdy - (c.y0 + c.y1) / 2 }
  }
  function tableHome(it: Item) {
    const b = localBox(it), w = b.x1 - b.x0
    const cx = clamp(lerp(G.fx0, G.fx1, it.u), G.fx0 + w / 2, Math.max(G.fx0 + w / 2, G.fx1 - w / 2))
    return { x: cx - (b.x0 + b.x1) / 2, y: G.top - 1 - b.y1 }
  }
  function pileLayout() {
    let y = G.top - 1
    for (const p of S.pile) { const b = localBox(p); p.hx = G.px - (b.x0 + b.x1) / 2; p.hy = y - b.y1; y -= b.y1 - b.y0 }
    return y
  }
  function home(it: Item) {
    if (it.zone === 'basket') return basketHome(it)
    if (it.zone === 'pile') return { x: it.hx, y: it.hy }
    return tableHome(it)
  }

  // ---------- messages ----------
  // Clear first, then set on the next frame, so a repeated message is announced again.
  let sayTimer = 0
  function say(msg: string) {
    liveEl.textContent = ''
    window.clearTimeout(sayTimer)
    sayTimer = window.setTimeout(() => { if (!stopped) liveEl.textContent = msg }, 30)
    markDirty()
  }
  let toastTimer = 0
  function toast(msg: string, ms: number) {
    toastEl.textContent = msg; toastEl.classList.add('laundry-on')
    const id = ++toastTimer
    later(ms, () => { if (id === toastTimer) toastEl.classList.remove('laundry-on') })
  }
  function front(it: Item) { const i = S.items.indexOf(it); if (i >= 0) { S.items.splice(i, 1); S.items.push(it) } }

  // ---------- the counter: plain text, so it reads as a count and nothing else ----------
  // It counts what has gone on the pile, the folded sheet every game starts with included, so the pile and the number
  // agree from the first frame. `S.count` stays the player's own stacks, which the first ghost waits on.
  const SEEDED = 1
  let hopAnim: Animation | null = null // the counter's hop in flight, so reduced motion turning on can finish it
  function setCount(n: number, hop?: boolean) {
    S.count = n
    countEl.textContent = T().folded(n + SEEDED)
    // A small hop; the Web Animations API, not a CSS @keyframes rule. Skipped under reduced motion.
    // Kept, so reduced motion turning on mid-hop can finish it (onMotion).
    if (hop && !RM && countEl.animate) hopAnim = countEl.animate([{ transform: 'none' }, { transform: 'translateY(-3px)', offset: 0.4 }, { transform: 'none' }], { duration: 280, easing: 'ease-out' })
  }
  function describe(it: Item) {
    const t = T()
    if (it.zone === 'basket') return t.item_basket
    if (it.step >= it.nf) return t.item_folded
    if (it.step > 0) return t.item_part_folded
    return t.item_table
  }
  function relabel(it: Item) { markDirty(); if (it.btn) it.btn.setAttribute('aria-label', describe(it)) }

  // Where on the table an item fits best: least overlap with the items already there, then nearest to `want`.
  function spotX(it: Item, want: number) {
    const b = localBox(it), w = b.x1 - b.x0, lo = G.fx0 + w / 2, hi = Math.max(lo, G.fx1 - w / 2)
    let best = clamp(want, lo, hi), bs = Infinity
    for (let i = 0; i <= 16; i++) {
      const x = lerp(lo, hi, i / 16)
      let ov = 0
      for (const o of S.items) {
        if (o === it || o.zone !== 'table') continue
        const ob = localBox(o), oc = o.x + (ob.x0 + ob.x1) / 2, ow = (ob.x1 - ob.x0) / 2
        ov += Math.max(0, Math.min(x + w / 2, oc + ow) - Math.max(x - w / 2, oc - ow))
      }
      const score = ov * 3 + Math.abs(x - want)
      if (score < bs) { bs = score; best = x }
    }
    return best
  }
  function freeX(it: Item) { return spotX(it, (G.fx0 + G.fx1) / 2) }
  function toTable(it: Item, cx: number) {
    it.zone = 'table'
    it.u = clamp((spotX(it, cx) - G.fx0) / (G.fx1 - G.fx0), 0, 1)
    front(it)
    const h = tableHome(it)
    tween(it, { x: h.x, y: h.y, rot: 0 }, 260, easeOut)
    if (it.crumple > 0) tween(it, { crumple: 0 }, 340, easeOut)
    relabel(it)
    topUp()
  }
  /** Where the item landed: a full basket sends it to the table. */
  function toBasket(it: Item, quiet?: boolean): 'basket' | 'table' {
    // The basket holds five. A return after a replacement has dropped in would make six on five slots (two items drawn
    // as one, two focus buttons), so the item goes to the table instead: straight through toTable, not dropAt, whose
    // table-full branch would send it back here. Even an over-full table is drawn; an over-full basket is not.
    if (basketCount(it) >= 5) { toTable(it, freeX(it)); if (!quiet) say(T().placed_table); return 'table' }
    it.zone = 'basket'
    const used = basketSlotsInUse(it)
    if (used.has(it.slot)) for (let k = 0; k < 5; k++) if (!used.has(k)) { it.slot = k; break }
    const h = basketHome(it)
    it.z = ++S.ztop
    tween(it, { x: h.x, y: h.y, rot: rnd(-0.3, 0.3), crumple: 1 }, 300, easeOut)
    relabel(it)
    if (!quiet) say(T().placed_basket)
    return 'basket'
  }
  function dropAt(it: Item, zone: Zone, cx: number) {
    if (zone === 'pile') {
      if (it.fold && it.step + it.queued >= it.nf) while (it.fold) finishFold(it, true)
      if (it.step >= it.nf && !it.fold) return stack(it)
      say(T().fold_first); toast(T().fold_first, 1400)
      zone = 'table'; cx = G.fx1
    }
    if (zone === 'basket' && it.step === 0 && !it.fold) return toBasket(it)
    // a full table refuses new items, so flat clothes never hide each other completely
    if (it.zone !== 'table' && it.step === 0) {
      const used = S.items.reduce((a, o) => (o !== it && o.zone === 'table' ? a + localBox(o).x1 - localBox(o).x0 : a), 0)
      const b = localBox(it)
      if (used + b.x1 - b.x0 > (G.fx1 - G.fx0) * 1.2) {
        // a full basket sends it to the table after all, and then that is what the player hears
        if (toBasket(it, true) === 'table') { say(T().placed_table); return }
        toast(T().table_full, 1400)
        say(T().table_full)
        return
      }
    }
    toTable(it, cx)
  }
  function stack(it: Item) {
    it.zone = 'pile'
    S.pile.push(it)
    pileLayout()
    tween(it, { x: it.hx, y: it.hy, rot: 0 }, 240, easeBack)
    // if the pile is still settling after an egg, move the others to their new slots now
    for (const p of S.pile) if (p !== it && Math.abs(p.y - p.hy) > 0.5) tween(p, { x: p.hx, y: p.hy }, 300, easeBack)
    setCount(S.count + 1, true)
    dropBtn(it)
    say(T().placed_pile)
    later(RM ? 40 : 300, () => { egg(); if (!S.settling) putAwayIfFull() })
    topUp()
  }

  // ---------- the egg: the sheet at the bottom of the pile slips out, turns into the Kiro ghost and floats away ----------
  // Every pile starts on a folded sheet. The first ghost comes on the player's first or second stack; after that each
  // stack has an EGG_CHANCE roll while a sheet is at the bottom.
  function egg() {
    const n = S.pile.length
    if (S.settling || n < 2 || S.pile[0].type !== 'sheet') return
    const roll = S.ghosts ? rand() < EGG_CHANCE : S.count >= S.firstGhost
    if (!opts.forceGhost && !roll) return
    S.ghosts++
    const sh = S.pile.shift() as Item, b = localBox(sh)
    sh.zone = 'egg'
    S.settling = true
    const w = b.x1 - b.x0, dist = w + 22
    const dir = V.x1 - (sh.x + b.x1) >= dist + 8 ? 1 : -1
    tween(sh, { x: sh.x + dir * dist, rot: dir * 0.05 }, 560, easeInOut, () => startCloth(sh))
    later(RM ? 0 : 300, () => {
      pileLayout()
      for (const p of S.pile) if (Math.abs(p.y - p.hy) > 0.5) tween(p, { x: p.hx, y: p.hy }, 360, easeBack)
    })
    say(T().ghost)
  }
  // The sheet shakes itself open (a ripple runs round its edge), then drapes into the ghost: the top settles first,
  // the hem last, with a little spring. Times in ms: unfold, billow, drape.
  const CLOTH = [450, 520, 950], CLOTH_MS = CLOTH[0] + CLOTH[1] + CLOTH[2]
  function startCloth(sh: Item) {
    S.settling = false
    const b = localBox(sh), cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2, lift = -85
    const rect = (w: number, h: number, dy: number) => resample([[cx - w / 2, cy + dy + h / 2], [cx + w / 2, cy + dy + h / 2], [cx + w / 2, cy + dy - h / 2], [cx - w / 2, cy + dy - h / 2]], NG)
    const A = rect(b.x1 - b.x0, b.y1 - b.y0, 0), B = rect(160, 100, lift)
    const g = ghostShape(cx, cy + lift - 4), C = align(B, g.pts), gb = boxOf([C])
    const nrm = B.map((p, i) => {
      const nq = B[(i + 1) % NG], r = B[(i + NG - 1) % NG], l = Math.hypot(nq[0] - r[0], nq[1] - r[1]) || 1
      let nx = (nq[1] - r[1]) / l, ny = (r[0] - nq[0]) / l
      if (nx * (p[0] - cx) + ny * (p[1] - cy - lift) < 0) { nx = -nx; ny = -ny }
      return [nx, ny]
    })
    sh.cloth = { A, B, C, nrm, ny: C.map(p => (p[1] - gb.y0) / (gb.y1 - gb.y0)), eyes: g.eyes, gc: [cx, cy + lift - 4], t0: performance.now() - (RM ? CLOTH_MS : 0) }
    tween(sh, { rot: 0 }, 300, easeOut)
  }
  function clothPts(K: Cloth, t: number): Pt[] {
    const tU = clamp(t / CLOTH[0], 0, 1), tD = clamp((t - CLOTH[0] - CLOTH[1]) / CLOTH[2], 0, 1)
    const amp = 9 * easeOut(tU) * (1 - easeInOut(tD)), ph = (t / 1000) * 9, ex = easeOut(clamp(tU * 1.4, 0, 1)), ey = easeInOut(tU)
    const lift = Math.sin(clamp((t - CLOTH[0]) / CLOTH[1], 0, 1) * PI) * -10
    return K.A.map((a, i) => {
      const b = K.B[i], c = K.C[i]
      let x = lerp(a[0], b[0], ex), y = lerp(a[1], b[1], ey) + lift * (1 - tD)
      const e = easeBack(clamp((tD - K.ny[i] * 0.45) / 0.55, 0, 1))
      x = lerp(x, c[0], e); y = lerp(y, c[1], e)
      const sa = (i / NG) * PI * 2, w = Math.sin(sa * 3 - ph) * amp + Math.sin(sa * 5 + ph * 1.3) * amp * 0.35
      return [x + K.nrm[i][0] * w, y + K.nrm[i][1] * w]
    })
  }
  // The ghost loops the loop over the table, swoops over the basket, then leaves off the top right.
  function flightPath(x0: number, y0: number) {
    const L = Math.max(V.x0 + 60, G.fx0 + 30), R = Math.min(V.x1 - 60, G.tx1 - 30), Tp = Math.max(V.y0 + 80, 40)
    const cx = lerp(L, R, rnd(0.3, 0.55)), cy = lerp(Tp, G.top - 120, 0.55), r = 44, turn = x0 > cx ? 1 : -1
    const pts: Pt[] = [[x0, y0], [lerp(x0, cx, 0.5), y0 - 50]]
    for (let k = 0; k <= 6; k++) { const a = PI / 2 + (k / 6) * PI * 2 * turn; pts.push([cx + Math.cos(a) * r, cy + Math.sin(a) * r]) }
    pts.push([G.bx + rnd(-30, 30), G.rimY - 140], [lerp(L, R, 0.55), Tp + 10], [V.x1 + 160, V.y0 - 150])
    const seg = [0]
    for (let i = 1; i < pts.length; i++) seg.push(seg[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
    return { pts, seg, len: seg[seg.length - 1] }
  }
  function pathAt(P: GhostFly['path'], d: number): Pt {
    const { pts, seg } = P
    let i = 1
    while (i < seg.length - 1 && seg[i] < d) i++
    const t = clamp((d - seg[i - 1]) / (seg[i] - seg[i - 1] || 1), 0, 1), t2 = t * t, t3 = t2 * t
    const p0 = pts[Math.max(0, i - 2)], p1 = pts[i - 1], p2 = pts[i], p3 = pts[Math.min(pts.length - 1, i + 1)]
    return [0, 1].map(k => 0.5 * (2 * p1[k] + (p2[k] - p0[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2 + (3 * p1[k] - p0[k] - 3 * p2[k] + p3[k]) * t3))
  }
  function startFlight(sh: Item) {
    const K = sh.cloth as Cloth, gx = K.gc[0], gy = K.gc[1], base = K.C.map(p => [p[0] - gx, p[1] - gy]), gb = boxOf([base])
    sh.cloth = null
    sh.x += gx; sh.y += gy; sh.rot = 0
    sh.ghost = { base, eyes: K.eyes.map(e => [e[0] - gx, e[1] - gy, e[2], e[3]]), top: gb.y0, h: gb.y1 - gb.y0,
      // the left-facing outline, point-matched to the right-facing one, so a turn morphs between them without collapsing
      mir: align(base, base.map(([x, y]) => [-x, y]).reverse()),
      t0: performance.now(), path: flightPath(sh.x, sh.y), look: [0, 0], blink: rnd(0.8, 1.8),
      ox: 0, oy: 0, ovx: 0, ovy: 0, vx: 0, vy: 0, face: 1, faceTo: 1, last: 0, wph: 0 }
    if (RM) retireGhost(sh)
  }
  // Reduced motion: a ghost stays put for a moment, then fades in one short step. One path for a ghost born under it and
  // one already flying when it is switched on (its flight stops under it, so nothing else would end it). The timer and
  // the fade run to completion even if the setting is switched off again meanwhile.
  const retiring = new WeakSet<Item>()
  function retireGhost(sh: Item) {
    if (retiring.has(sh)) return
    retiring.add(sh)
    later(800, () => tween(sh, { alpha: 0 }, 300, easeOut, () => gone(sh), true))
  }
  // Where the pointer is (world units), so the flying ghost can keep out of its way.
  const PTR = { x: 0, y: 0, on: false }
  function ghostStep(sh: Item) {
    const Gh = sh.ghost as GhostFly, t = (now - Gh.t0) / 1000
    if (RM) return
    const dt = Gh.last ? clamp((now - Gh.last) / 1000, 0, 0.05) : 0
    Gh.last = now
    const d = 170 * (t > 1 ? t - 0.5 : (t * t) / 2) // eases up to speed over the first second
    const p = pathAt(Gh.path, d)
    // dodge: a push away from the pointer inside 120 units, and a damped spring back to the flight path
    let fx = -26 * Gh.ox - 9 * Gh.ovx, fy = -26 * Gh.oy - 9 * Gh.ovy
    if (PTR.on) {
      const dx = sh.x - PTR.x, dy = sh.y - PTR.y, dist = Math.hypot(dx, dy) || 1
      if (dist < 120) { const push = 9000 * (1 - dist / 120) ** 2; fx += (dx / dist) * push; fy += (dy / dist) * push }
    }
    Gh.ovx += fx * dt; Gh.ovy += fy * dt; Gh.ox += Gh.ovx * dt; Gh.oy += Gh.ovy * dt
    const ol = Math.hypot(Gh.ox, Gh.oy)
    if (ol > 160) { Gh.ox *= 160 / ol; Gh.oy *= 160 / ol }
    const nx = p[0] + Math.sin(t * 1.7) * 3 + Gh.ox, ny = p[1] + Math.sin(t * 3.1) * 4 + Gh.oy
    if (dt > 0) {
      const k = 1 - Math.exp(-dt * 8)
      Gh.vx = lerp(Gh.vx, (nx - sh.x) / dt, k); Gh.vy = lerp(Gh.vy, (ny - sh.y) / dt, k)
    }
    sh.x = nx; sh.y = ny
    // face the way it is going: turn about the vertical axis (eased, with a little hysteresis), pitch a little with the climb
    if (Math.abs(Gh.vx) > 40 && Math.sign(Gh.vx) !== Gh.faceTo) Gh.faceTo = Math.sign(Gh.vx)
    const kf = 1 - Math.exp(-dt * 6), sp = Math.hypot(Gh.vx, Gh.vy) || 1
    Gh.face = lerp(Gh.face, Gh.faceTo, kf)
    Gh.wph += dt * 7.5 * Gh.face
    const pitch = clamp(Math.atan2(Gh.vy, Math.abs(Gh.vx) + 1) * 0.35, -0.3, 0.3) * Gh.face
    sh.rot = lerp(sh.rot, pitch + Math.sin(t * 2.2) * 0.04, 1 - Math.exp(-dt * 5))
    Gh.look = [lerp(Gh.look[0], (Math.abs(Gh.vx) / sp) * 2.4, kf), lerp(Gh.look[1], (Gh.vy / sp) * 1.8, kf)]
    if (d >= Gh.path.len || t > 30) gone(sh)
  }
  function drawEyes(eyes: number[][], open: number, lx: number, ly: number) {
    if (open <= 0) return
    ctx.fillStyle = ink
    for (const e of eyes) { ctx.beginPath(); ctx.ellipse(e[0] + lx, e[1] + ly, e[2], Math.max(0.6, e[3] * open), 0, 0, PI * 2); ctx.fill() }
  }
  function gone(it: Item) { it.zone = 'gone'; const i = S.items.indexOf(it); if (i >= 0) S.items.splice(i, 1); markDirty() }
  // A pile taller than the scene allows is put away to the right, and the next pile starts on a folded sheet. A pile
  // is also put away before its next item could reach the counter's row, so the counter never covers the pile.
  function putAwayIfFull() {
    const top = pileTop()
    if (G.top - top < G.pmax && top - 62 > G.hud + 6) return
    const pile = S.pile
    S.pile = []
    for (const p of pile) p.zone = 'leaving'
    say(T().pile_full)
    if (RM) { for (const p of pile) gone(p); seedPile(); return }
    const dx = V.x1 - G.px + 140
    for (const p of pile) tween(p, { x: p.x + dx }, 700, easeIn, () => gone(p))
    later(450, seedPile)
  }
  function foldNow(it: Item) {
    it.crumple = 0
    while (it.step < it.nf) { const f = it.T.folds[it.step]; it.queued = 1; it.fold = { f, t0: 0, ext: creaseExtent(it.layers, f) }; finishFold(it, true) }
  }
  function seedPile() {
    const sh = makeItem('sheet')
    foldNow(sh)
    sh.zone = 'pile'
    S.items.push(sh)
    S.pile.unshift(sh)
    pileLayout()
    sh.x = sh.hx; sh.y = S.started && !RM ? V.y0 - 80 : sh.hy
    tween(sh, { y: sh.hy }, 560, easeBack)
    for (const p of S.pile) if (p !== sh) tween(p, { x: p.hx, y: p.hy }, 300, easeBack)
  }
  // The basket never runs out: whenever it holds fewer than five, a new crumpled item drops in from above.
  let bag: ItemType[] = []
  function nextType() { if (!bag.length) bag = shuffle(ORDER.slice(), rand); return bag.pop() as ItemType }
  /** Items in the basket, plus one picked up from it (by pointer or keyboard) that is still in the hand. */
  function basketCount(except?: Item) {
    const out = (S.drag && S.drag.from.zone === 'basket' && S.drag.it !== except ? 1 : 0) + (S.hold && S.hold.from.zone === 'basket' && S.hold.it !== except ? 1 : 0)
    return S.items.filter(o => o !== except && o.zone === 'basket').length + out
  }
  /** Basket slots taken, including the one an item in the hand came from: a cancel puts it straight back there. */
  function basketSlotsInUse(except?: Item) {
    const used = new Set(S.items.filter(o => o !== except && o.zone === 'basket').map(o => o.slot))
    for (const h of [S.drag, S.hold]) if (h && h.from.zone === 'basket' && h.it !== except) used.add(h.from.slot)
    return used
  }
  function topUp() {
    const have = basketCount() + S.incoming
    for (let k = have; k < 5; k++) { S.incoming++; later(450 + (k - have) * 160, dropIn) }
  }
  function dropIn() {
    S.incoming--
    // counted again now: an item returned to the basket while this one was on its way may have filled it
    if (basketCount() >= 5) return
    const used = basketSlotsInUse()
    const free = [0, 1, 2, 3, 4].filter(k => !used.has(k))
    const it = makeItem(nextType())
    it.slot = free.length ? free[Math.floor(rand() * free.length)] : Math.floor(rand() * 5)
    it.sdy = rnd(-5, 5); it.rot = rnd(-0.4, 0.4); it.z = ++S.ztop
    const h = basketHome(it)
    it.x = h.x; it.y = RM ? h.y : V.y0 - 90
    S.items.push(it)
    tween(it, { y: h.y }, 620, easeBack)
    addBtn(it)
    if (document.activeElement === root && it.btn) it.btn.focus()
  }
  function fillBasket(drop?: boolean) {
    // the first basket: the sheet lies on top of the heap, in the middle; the rest are shuffled underneath
    const types = shuffle<ItemType>(ORDER.filter(t => t !== 'sheet'), rand).concat('sheet'), slots = shuffle([0, 1, 3, 4], rand).concat(2)
    const fresh = types.map((ty, i) => {
      const it = makeItem(ty)
      it.slot = slots[i]; it.sdy = ty === 'sheet' ? -9 : rnd(-5, 5); it.rot = rnd(-0.4, 0.4); it.z = ty === 'sheet' ? 9 : it.slot
      const h = basketHome(it)
      it.x = h.x; it.y = h.y
      S.items.push(it)
      if (drop && !RM) {
        it.y = V.y0 - 90 - it.slot * 20
        later(it.slot * 90, () => { if (it.zone === 'basket') tween(it, { y: basketHome(it).y }, 620, easeBack) })
      }
      return it
    })
    fresh.sort((a, b) => a.slot - b.slot).forEach(addBtn)
  }
  // Start over without reloading: a full basket, a new pile on a folded sheet, and the count back to 0.
  function restart() {
    if (stopped) return
    if (S.drag) { try { cv.releasePointerCapture(S.drag.id) } catch { /* already released */ } S.drag = null }
    cv.style.cursor = ''
    S.hold = null
    const a = document.activeElement, refocus = keysEl.contains(a) || a === root
    tweens.length = 0
    timers.length = 0
    S.items = []; S.pile = []; S.incoming = 0; S.settling = false; S.ztop = 10
    S.ghosts = 0; S.firstGhost = rand() < 0.5 ? 1 : 2
    bag = []
    keysEl.textContent = ''
    ++toastTimer; toastEl.classList.remove('laundry-on')
    setCount(0)
    fillBasket(true)
    seedPile()
    if (refocus) { const f = S.items.find(o => o.btn); (f && f.btn ? f.btn : root).focus() }
    say(T().started_over)
    markDirty()
  }
  // R starts over while focus is inside the game. Listened for on the game itself, so a host that keeps keys from
  // reaching the page (the update dialog does) still lets it through.
  on(root, 'keydown', (e: KeyboardEvent) => {
    if ((e.key !== 'r' && e.key !== 'R') || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return
    e.preventDefault()
    restart()
  })
  function activate(it: Item) {
    if (it.zone === 'basket') { dropAt(it, 'table', freeX(it)); if ((it.zone as Zone) === 'table') say(T().placed_table); return }
    if (it.zone !== 'table') return
    front(it)
    if (it.step + it.queued < it.nf) { it.queued++; if (!it.fold) startFold(it); return }
    if (!it.fold) {
      say(T().fold_done)
      if (!RM) { const x = it.x; tween(it, { x: x + 6 }, 110, easeOut, () => tween(it, { x }, 170, easeOut)) }
    }
  }
  function startFold(it: Item) {
    cancelKeys(it, ['crumple'])
    it.crumple = 0
    const f = it.T.folds[it.step]
    it.fold = { f, t0: performance.now(), ext: creaseExtent(it.layers, f) }
    if (RM) finishFold(it)
    else kick()
  }
  function finishFold(it: Item, quiet?: boolean) {
    const F = it.fold as NonNullable<Item['fold']>, f = F.f, res = foldLayers(it.layers, f, -1), layers = res.stat.concat(res.flap)
    const lo = F.ext[0], hi = F.ext[1], len = hi - lo
    if (len > 8 && layers.length) {
      // a crease mark just inside the new folded edge, on the layer that is now on top
      const off = f.at - f.dir * 3.5, a = lo + len * 0.16, b = hi - len * 0.16
      layers[layers.length - 1].lines.push(densify(f.ax === 0 ? [[off, a], [off, b]] : [[a, off], [b, off]], 6, false, 0.3, rand))
    }
    it.layers = layers
    it.step++; it.queued--; it.fold = null
    if (it.step >= it.nf) {
      const b = localBox(it)
      it.layers = [{ pts: densify([[b.x0, b.y0], [b.x1, b.y0], [b.x1, b.y1], [b.x0, b.y1]], 6, true, 0.45, rand),
        lines: finalLines(it.type, b).map(l => densify(l, 6, false, 0.3, rand)) }]
    }
    relabel(it)
    if (!quiet) say(it.step >= it.nf ? T().fold_done : T().fold_step)
    if (it.queued > 0) startFold(it)
  }

  // ---------- drawing ----------
  function traceClosed(pts: Pt[]) {
    const n = pts.length
    if (n < 2) return
    const m = (p: Pt, nq: Pt) => [(p[0] + nq[0]) / 2, (p[1] + nq[1]) / 2]
    const st = m(pts[n - 1], pts[0])
    ctx.moveTo(st[0], st[1])
    for (let i = 0; i < n; i++) { const p = pts[i], mq = m(p, pts[(i + 1) % n]); ctx.quadraticCurveTo(p[0], p[1], mq[0], mq[1]) }
    ctx.closePath()
  }
  function traceOpen(pts: Pt[]) {
    const n = pts.length
    if (n < 2) return
    ctx.moveTo(pts[0][0], pts[0][1])
    for (let i = 1; i < n - 1; i++) { const p = pts[i], nq = pts[i + 1]; ctx.quadraticCurveTo(p[0], p[1], (p[0] + nq[0]) / 2, (p[1] + nq[1]) / 2) }
    ctx.lineTo(pts[n - 1][0], pts[n - 1][1])
  }
  let drawn: Pt[][] = []
  function shape(pts: Pt[]) { drawn.push(pts); ctx.beginPath(); traceClosed(pts); ctx.fillStyle = paper; ctx.fill(); ctx.stroke() }
  function strokes(ls: Pt[][], a: number) {
    if (!ls.length || a <= 0.01) return
    const g = ctx.globalAlpha, w = ctx.lineWidth
    ctx.globalAlpha = g * Math.min(1, a); ctx.lineWidth = w * 0.8
    ctx.beginPath()
    for (const l of ls) traceOpen(l)
    ctx.stroke()
    ctx.globalAlpha = g; ctx.lineWidth = w
  }
  function drawLayers(it: Item): Box {
    const c = it.crumple
    if (c > 0.001 && it.step === 0 && it.layers.length === 1) {
      const a = it.layers[0].pts, b = it.cpts
      const pts = a.map((p, i) => [lerp(p[0], b[i][0], c), lerp(p[1], b[i][1], c)])
      shape(pts)
      const fl = it.layers[0].lines, cl = it.clines
      strokes(fl.map((l, i) => l.map((p, j) => [lerp(p[0], cl[i][j][0], c), lerp(p[1], cl[i][j][1], c)])), 1)
      strokes(it.wr, c)
      return boxOf([pts])
    }
    for (const L of it.layers) { shape(L.pts); strokes(L.lines, 1) }
    return localBox(it)
  }
  function drawFolding(it: Item): Box {
    const F = it.fold as NonNullable<Item['fold']>, t = clamp((now - F.t0) / FOLD_MS, 0, 1)
    const g = clamp(t / 0.3, 0, 1), e = easeInOut(clamp((t - 0.3) / 0.7, 0, 1))
    const res = foldLayers(it.layers, F.f, Math.cos(PI * e))
    for (const L of res.stat) { shape(L.pts); strokes(L.lines, 1) }
    // the crease appears first, then the flap turns over it
    const a = F.ext[0], b = lerp(F.ext[0], F.ext[1], g), at = F.f.at
    ctx.save(); ctx.setLineDash([4, 3]); ctx.beginPath()
    if (F.f.ax === 0) { ctx.moveTo(at, a); ctx.lineTo(at, b) } else { ctx.moveTo(a, at); ctx.lineTo(b, at) }
    ctx.stroke(); ctx.restore()
    for (const L of res.flap) { shape(L.pts); strokes(L.lines, 1) }
    return boxOf(res.stat.concat(res.flap).map(l => l.pts))
  }
  function drawCloth(it: Item): Box {
    const K = it.cloth as Cloth, t = Math.min(now - K.t0, CLOTH_MS), pts = clothPts(K, t)
    shape(pts)
    strokes(it.layers[0].lines, 1 - t / 220)
    const tD = clamp((t - CLOTH[0] - CLOTH[1]) / CLOTH[2], 0, 1)
    drawEyes(K.eyes, clamp((tD - 0.72) / 0.28, 0, 1), 0, 0)
    return boxOf([pts])
  }
  function drawGhostFly(it: Item): Box {
    // the breathing and the hem wave fade in over half a second, so the hand-over from the drape is seamless
    const Gh = it.ghost as GhostFly, t = (now - Gh.t0) / 1000, m = RM ? 0 : Math.min(1, t / 0.5), br = 1 + Math.sin(t * 3.2) * 0.035 * m
    // f is the turn about the vertical axis: 1 faces right (the mark's own pose), -1 faces left. Mid-turn the round
    // body faces the viewer: the outline stays wide (a little narrower) and the eyes slide across the face.
    const f = Gh.face, af = Math.abs(f), u = (1 - f) / 2, narrow = 1 - 0.12 * (1 - af)
    const pts = Gh.base.map(([x, y], i) => {
      const mx = lerp(x, Gh.mir[i][0], u), my = lerp(y, Gh.mir[i][1], u)
      const w = clamp(((my - Gh.top) / Gh.h - 0.55) / 0.45, 0, 1) * m, wave = Math.sin(mx * 0.16 + Gh.wph) * 2.6 * w
      return [(mx * (2 - br) + wave * 0.5) * narrow, my * br + wave]
    })
    shape(pts)
    const bt = (t + Gh.blink) % 2.9, open = bt < 0.16 ? Math.abs(bt - 0.08) / 0.08 : 1
    const E = Gh.eyes, ec = (E[0][0] + E[1][0]) / 2, hs = (Math.abs(E[1][0] - E[0][0]) / 2) * (1 + 0.35 * (1 - af))
    const eyes = [[(ec * f - hs) * narrow, E[0][1], E[0][2], E[0][3]], [(ec * f + hs) * narrow, E[1][1], E[1][2], E[1][3]]]
    drawEyes(eyes, RM ? 1 : open, Gh.look[0] * f, Gh.look[1])
    return boxOf([pts])
  }
  function drawItem(it: Item) {
    let bob = 0, br = 0
    if (it.zone === 'basket' && !RM && !S.drag && !S.hold) {
      const t = now / 1000
      bob = Math.sin(t * 1.7 + it.ph) * 1.3; br = Math.sin(t * 1.25 + it.ph) * 0.012
    }
    const r = it.rot + br, y = it.y + bob
    ctx.save()
    ctx.globalAlpha = it.alpha
    ctx.translate(it.x, y)
    if (r) ctx.rotate(r)
    drawn = []
    const lb = it.ghost ? drawGhostFly(it) : it.cloth ? drawCloth(it) : it.fold ? drawFolding(it) : drawLayers(it)
    ctx.restore()
    it.hp = drawn; it.tf = { x: it.x, y, r }
    // world-space box for hit tests and the keyboard focus ring (rotation-aware)
    const c = Math.cos(r), sn = Math.sin(r), xs: number[] = [], ys: number[] = []
    for (const [px, py] of [[lb.x0, lb.y0], [lb.x1, lb.y0], [lb.x1, lb.y1], [lb.x0, lb.y1]]) { xs.push(it.x + px * c - py * sn); ys.push(y + px * sn + py * c) }
    it.wb = { x0: Math.min(...xs) - 5, y0: Math.min(...ys) - 5, x1: Math.max(...xs) + 5, y1: Math.max(...ys) + 5 }
  }
  function drawFloor() {
    ctx.beginPath(); ctx.moveTo(V.x0, G.floor); ctx.lineTo(V.x1, G.floor); ctx.stroke()
    ctx.save(); ctx.globalAlpha = 0.55; ctx.lineWidth = LW * 0.8; ctx.beginPath()
    for (const d of DASH) if (d.x > V.x0 && d.x < V.x1) { ctx.moveTo(d.x, G.floor + d.y); ctx.lineTo(d.x + d.l, G.floor + d.y) }
    ctx.stroke(); ctx.restore()
  }
  function drawTable() {
    const { tx0, tx1, top, floor } = G
    ctx.fillStyle = paper
    ctx.beginPath()
    ctx.rect(tx0 + 22, top + 10, 6, floor - top - 10)
    ctx.rect(tx1 - 28, top + 10, 6, floor - top - 10)
    ctx.fill(); ctx.stroke()
    ctx.beginPath(); ctx.moveTo(tx0 + 28, top + 24); ctx.lineTo(tx1 - 28, top + 24); ctx.stroke()
    ctx.beginPath()
    if (ctx.roundRect) ctx.roundRect(tx0, top, tx1 - tx0, 10, 3); else ctx.rect(tx0, top, tx1 - tx0, 10)
    ctx.fill(); ctx.stroke()
  }
  function drawBasket() {
    const { bx, bw, bh, floor } = G, rim = G.rimY, bb = bw * 0.42
    const L = (t: number) => lerp(bx - bw / 2, bx - bb, t), R = (t: number) => lerp(bx + bw / 2, bx + bb, t), Y = (t: number) => rim + bh * t
    ctx.fillStyle = paper
    ctx.beginPath(); ctx.moveTo(L(0), rim); ctx.lineTo(R(0), rim); ctx.lineTo(R(1), floor); ctx.lineTo(L(1), floor); ctx.closePath()
    ctx.fill(); ctx.stroke()
    ctx.beginPath(); ctx.moveTo(L(0.11), Y(0.11)); ctx.lineTo(R(0.11), Y(0.11)); ctx.stroke()
    ctx.save(); ctx.lineWidth = LW * 0.8; ctx.beginPath()
    for (const t of [0.44, 0.74]) { ctx.moveTo(L(t) + 3, Y(t)); ctx.lineTo(R(t) - 3, Y(t)) }
    ;[[0.11, 0.44], [0.44, 0.74], [0.74, 1]].forEach(([a, b], row) => {
      const m = (a + b) / 2, xl = L(m) + 12, xr = R(m) - 12, n = Math.max(2, Math.round((xr - xl) / 15))
      for (let i = 0; i <= n; i++) if ((i + row) % 2 === 0) { const x = lerp(xl, xr, i / n); ctx.moveTo(x, Y(a) + 5); ctx.lineTo(x, Y(b) - 5) }
    })
    ctx.stroke(); ctx.restore()
    ctx.beginPath()
    for (const sx of [-1, 1]) { const hx = bx + sx * (bw / 2 - 30); ctx.moveTo(hx + 11, Y(0.27)); ctx.ellipse(hx, Y(0.27), 11, 4, 0, 0, PI * 2) }
    ctx.stroke()
  }
  function pileTop() { let y = G.top - 1; for (const p of S.pile) { const b = localBox(p); y -= b.y1 - b.y0 } return y }
  function drawPreview(it: Item) {
    const b = localBox(it), w = b.x1 - b.x0, h = b.y1 - b.y0, t = pileTop()
    ctx.save(); ctx.globalAlpha = 0.5; ctx.setLineDash([4, 4])
    ctx.beginPath(); ctx.rect(G.px - w / 2, t - h, w, h); ctx.stroke()
    ctx.restore()
  }
  /** The "Pile" word's box, in scene units: drawn here and counted as part of the pile drop target in zoneAt. At least 11 screen px tall at any scale. */
  function pileLabelRect() {
    const size = Math.max(14, 11 / s)
    // measured in the drawn font, so a full-width word (zh-CN, ja, ko) is as wide to the hit test as it is on screen
    ctx.save(); ctx.font = pileLabelFont(size); const w = ctx.measureText(T().pile).width; ctx.restore()
    // On the tabletop beside the stack, clear of the pile's widest item (the pile target is 62 either side of px).
    // Where the view has no room to its right, just above the stack: still the pile's own target, never the table's.
    const right = G.px + 62 + 4 + w / 2
    // beside: the word sits on the table and counts as pile; above: the pile column already covers it, and a wide word
    // (German at the font floor on a narrow card) must not turn the table under its overhang into pile
    return right + w / 2 <= V.x1 - 4 ? { size, w, x: right, y: G.top - 2 - size, beside: true } : { size, w, x: G.px, y: pileTop() - 6 - size, beside: false }
  }
  function pileLabelFont(size: number) { return `${size}px ${getComputedStyle(root).fontFamily || 'monospace'}` }
  function drawPileLabel() {
    const L = pileLabelRect()
    ctx.save(); ctx.fillStyle = ink; ctx.globalAlpha = 0.75; ctx.textAlign = 'center'; ctx.textBaseline = 'top'
    ctx.font = pileLabelFont(L.size)
    ctx.fillText(T().pile, L.x, L.y)
    ctx.restore()
  }
  function draw() {
    const dpr = window.devicePixelRatio || 1
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.clearRect(0, 0, cv.width, cv.height)
    ctx.setTransform(dpr * s, 0, 0, dpr * s, dpr * ox, dpr * oy)
    ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.strokeStyle = ink; ctx.lineWidth = LW
    const by = (z: Zone) => S.items.filter(o => o.zone === z)
    drawFloor()
    drawTable()
    drawPileLabel()
    by('basket').sort((a, b) => a.z - b.z).forEach(drawItem)
    drawBasket()
    S.pile.forEach(drawItem)
    by('leaving').forEach(drawItem)
    by('table').forEach(drawItem)
    const held = S.hold ? S.hold.it : S.drag && S.drag.moved ? S.drag.it : null
    const target = S.hold ? S.hold.zone : S.drag && S.drag.moved ? S.drag.zone : null
    if (held && target === 'pile' && held.step >= held.nf) drawPreview(held)
    by('egg').forEach(drawItem)
    if (held) drawItem(held)
  }
  const btnKey = new WeakMap<HTMLButtonElement, string>()
  function placeBtns() {
    for (const it of S.items) {
      const b = it.btn, w = it.wb
      if (!b || !w) continue
      const l = ox + w.x0 * s, t = oy + w.y0 * s, bw = (w.x1 - w.x0) * s, bh = (w.y1 - w.y0) * s
      const key = `${l | 0},${t | 0},${bw | 0},${bh | 0}`
      if (btnKey.get(b) === key) continue
      btnKey.set(b, key)
      b.style.transform = `translate(${l}px,${t}px)`; b.style.width = `${bw}px`; b.style.height = `${bh}px`
    }
  }

  // ---------- keyboard ----------
  // Each item has an invisible button laid over its drawing: the canvas cannot take focus per item, so these are the
  // focus stops a keyboard or screen-reader user moves between. They are created here, not in React, because the
  // canvas loop owns the items they follow.
  let lastKey = 0
  function addBtn(it: Item) {
    const b = document.createElement('button')
    b.type = 'button'
    b.dataset.type = it.type
    b.addEventListener('keydown', e => onKey(e, it))
    b.addEventListener('keyup', e => { if (e.key === ' ') e.preventDefault() })
    // screen readers in browse mode send a plain click instead of key events
    // (one per item, so a folded item's click puts it on the pile: browse mode has no pick-up-and-drop gesture)
    b.addEventListener('click', e => {
      if (e.detail !== 0 || performance.now() - lastKey <= 300 || S.drag) return
      if (it.zone === 'table' && it.step >= it.nf && !it.fold) dropAt(it, 'pile', G.fx1)
      else activate(it)
    })
    b.addEventListener('focus', () => { if (b.matches(':focus-visible')) hintEl.classList.add('laundry-on') })
    b.addEventListener('blur', () => {
      if (S.hold && S.hold.it === it) kbCancel()
      window.setTimeout(() => { if (!keysEl.contains(document.activeElement)) hintEl.classList.remove('laundry-on') }, 0)
    })
    keysEl.appendChild(b)
    it.btn = b
    relabel(it)
  }
  function dropBtn(it: Item) {
    const b = it.btn
    if (!b) return
    const had = document.activeElement === b
    it.btn = null
    if (had) {
      const next = S.items.find(o => o.btn && o.zone === 'table') || S.items.find(o => o.btn)
      ;(next && next.btn ? next.btn : root).focus()
    }
    b.remove()
  }
  function zonesFor(it: Item): Zone[] { return it.step === 0 && !it.fold ? ['basket', 'table', 'pile'] : ['table', 'pile'] }
  function heldPos(it: Item, zone: Zone) {
    const b = it.crumple > 0.5 ? it.cbox : localBox(it), cx = (b.x0 + b.x1) / 2
    if (zone === 'basket') return { x: G.bx - cx, y: G.rimY - 34 - b.y1 }
    if (zone === 'pile') return { x: G.px - cx, y: pileTop() - 22 - b.y1 }
    const h = tableHome(it)
    return { x: h.x, y: h.y - 22 }
  }
  function overText(z: Zone) { const t = T(); return z === 'basket' ? t.over_basket : z === 'pile' ? t.over_pile : t.over_table }
  function kbPick(it: Item) {
    if (it.zone !== 'basket' && it.zone !== 'table') return
    const zs = zonesFor(it), from = { zone: it.zone, u: it.u, slot: it.slot }
    S.hold = { it, from, zone: zs[Math.min(zs.indexOf(it.zone) + 1, zs.length - 1)] }
    it.zone = 'held'
    if (from.zone === 'basket') it.u = (freeX(it) - G.fx0) / (G.fx1 - G.fx0)
    kbShow()
    say(`${T().picked_up} ${overText(S.hold.zone)}`)
  }
  function kbShow() { const h = S.hold as Hold, p = heldPos(h.it, h.zone); tween(h.it, { x: p.x, y: p.y, rot: 0 }, 180, easeOut) }
  function kbMove(d: number) {
    const h = S.hold as Hold, zs = zonesFor(h.it), i = clamp(zs.indexOf(h.zone) + d, 0, zs.length - 1)
    if (zs[i] === h.zone) return
    h.zone = zs[i]
    kbShow()
    say(overText(h.zone))
  }
  function kbDrop() {
    const h = S.hold as Hold, it = h.it
    S.hold = null
    it.zone = h.from.zone
    dropAt(it, h.zone, lerp(G.fx0, G.fx1, it.u))
    if (h.zone === 'table' && it.zone === 'table') say(describe(it))
  }
  function kbCancel() {
    const h = S.hold as Hold, it = h.it
    S.hold = null
    it.zone = h.from.zone; it.u = h.from.u; it.slot = h.from.slot
    const p = home(it)
    tween(it, { x: p.x, y: p.y, rot: it.zone === 'basket' ? rnd(-0.3, 0.3) : 0 }, 200, easeOut)
    say(T().put_back)
  }
  function onKey(e: KeyboardEvent, it: Item) {
    const k = e.key
    lastKey = performance.now()
    if (S.hold && S.hold.it === it) {
      if (k === ' ' || k === 'Enter') { e.preventDefault(); if (k === ' ') kbDrop() }
      else if (k === 'Escape') { e.preventDefault(); e.stopPropagation(); kbCancel() }
      else if (k === 'ArrowLeft' || k === 'ArrowUp') { e.preventDefault(); kbMove(-1) }
      else if (k === 'ArrowRight' || k === 'ArrowDown') { e.preventDefault(); kbMove(1) }
      return
    }
    if (S.drag) return
    if (k === ' ') { e.preventDefault(); if (S.hold) kbCancel(); kbPick(it) }
    else if (k === 'Enter') { e.preventDefault(); if (S.hold) kbCancel(); activate(it) }
  }

  // ---------- pointer (mouse, pen, touch) ----------
  function toWorld(e: { clientX: number; clientY: number }): Pt { const r = cv.getBoundingClientRect(); return [(e.clientX - r.left - ox) / s, (e.clientY - r.top - oy) / s] }
  function inside(pts: Pt[], x: number, y: number) {
    let c = false
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const a = pts[i], b = pts[j]
      if ((a[1] > y) !== (b[1] > y) && x < ((b[0] - a[0]) * (y - a[1])) / (b[1] - a[1]) + a[0]) c = !c
    }
    return c
  }
  // Hit the outline you can see, topmost item first, with slack for fingers: at least 5 scene units and at least
  // 12 CSS pixels, so a small embed (a card at 320 px) still gives every item a target about 24 px across.
  function hits(o: Item, x: number, y: number) {
    const k = Math.max(5, 12 / s)
    if (!o.wb || x < o.wb.x0 - k || x > o.wb.x1 + k || y < o.wb.y0 - k || y > o.wb.y1 + k) return false
    const t = o.tf, c = Math.cos(-t.r), sn = Math.sin(-t.r)
    for (const [ddx, ddy] of [[0, 0], [k, 0], [-k, 0], [0, k], [0, -k]]) {
      const dx = x + ddx - t.x, dy = y + ddy - t.y, lx = dx * c - dy * sn, ly = dx * sn + dy * c
      if (o.hp.some(p => inside(p, lx, ly))) return true
    }
    return false
  }
  function hitTest(x: number, y: number) {
    const c = S.items.filter(o => o.zone === 'table').reverse()
      .concat(S.items.filter(o => o.zone === 'basket').sort((a, b) => b.z - a.z))
    return c.find(o => hits(o, x, y)) || null
  }
  function zoneAt(x: number, y: number): Zone {
    const L = pileLabelRect()
    if ((Math.abs(x - G.px) < 62 && y < G.top + 24 && y > pileTop() - 110) || (L.beside && Math.abs(x - L.x) <= L.w / 2 && y >= L.y && y <= L.y + L.size)) return 'pile'
    if (Math.abs(x - G.bx) < G.bw / 2 + 16 && y > G.rimY - 70) return 'basket'
    return 'table'
  }
  on(cv, 'pointerdown', (e: PointerEvent) => {
    { const [px, py] = toWorld(e); PTR.x = px; PTR.y = py; PTR.on = true }
    if (S.drag || (e.pointerType === 'mouse' && e.button !== 0)) return
    const [x, y] = toWorld(e), it = hitTest(x, y)
    if (!it) return
    if (S.hold) kbCancel()
    e.preventDefault()
    try { cv.setPointerCapture(e.pointerId) } catch { /* capture is optional */ }
    S.drag = { it, id: e.pointerId, sx: e.clientX, sy: e.clientY, dx: it.x - x, dy: it.y - y, moved: false, zone: it.zone, from: { zone: it.zone, u: it.u, slot: it.slot } }
    kick()
  })
  on(cv, 'pointermove', (e: PointerEvent) => {
    const d = S.drag, [x, y] = toWorld(e)
    PTR.x = x; PTR.y = y; PTR.on = true
    if (!d) { cv.style.cursor = hitTest(x, y) ? 'grab' : ''; return }
    if (e.pointerId !== d.id) return
    if (!d.moved) {
      if (Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < 6) return
      d.moved = true
      cancelKeys(d.it, ['x', 'y'])
      d.it.zone = 'drag'
      cv.style.cursor = 'grabbing'
    }
    d.it.x = x + d.dx; d.it.y = y + d.dy
    d.zone = zoneAt(x, y)
    markDirty()
  })
  function endDrag(e: { pointerId: number; clientX?: number; clientY?: number }, cancel: boolean) {
    const d = S.drag
    if (!d || e.pointerId !== d.id) return
    S.drag = null
    cv.style.cursor = ''
    const it = d.it
    if (!d.moved) { if (!cancel) activate(it); return }
    it.zone = d.from.zone
    if (cancel) { it.u = d.from.u; it.slot = d.from.slot; const p = home(it); tween(it, { x: p.x, y: p.y }, 220, easeOut); return }
    // The item's centre now, from its position and shape: `it.wb` is the last drawn frame's box, a move behind a release
    // that lands before the next frame.
    const [x, y] = toWorld(e as PointerEvent), b = localBox(it), z = zoneAt(x, y)
    dropAt(it, z, it.x + (b.x0 + b.x1) / 2)
    if (z === 'table' && d.from.zone === 'basket' && it.zone === 'table') say(T().placed_table)
  }
  on(cv, 'pointerup', (e: PointerEvent) => { if (e.pointerType !== 'mouse') PTR.on = false; endDrag(e, false) })
  on(cv, 'pointercancel', (e: PointerEvent) => { PTR.on = false; endDrag(e, true) })
  on(cv, 'pointerleave', () => { if (!S.drag) PTR.on = false })
  // Capture phase: a host that stops keys from bubbling (the update overlay does) must not hide Escape from a drag in progress.
  // It is consumed when it cancels a drag, so a host's own Escape (the update dialog closes and snoozes) never sees it;
  // with no drag, Escape passes through untouched.
  on(window, 'keydown', (e: KeyboardEvent) => {
    if (e.key !== 'Escape' || !S.drag || !S.drag.moved) return
    e.preventDefault(); e.stopPropagation()
    endDrag({ pointerId: S.drag.id }, true)
  }, true)

  // ---------- loop ----------
  // Full rate while anything moves; about 15 fps for the idle bob; under reduced motion an idle scene requests no frames.
  let lastDraw = -1e9
  function frame(t: number) {
    raf = 0
    if (stopped) return
    now = t
    runTimers()
    runTweens()
    for (const it of S.items) if (it.fold && now - it.fold.t0 >= FOLD_MS) finishFold(it)
    for (const it of S.items.slice()) {
      if (it.cloth && now - it.cloth.t0 >= CLOTH_MS) startFlight(it)
      if (it.ghost) ghostStep(it)
    }
    const busy = tweens.length || timers.length || S.drag || S.hold || S.items.some(o => o.fold || o.cloth || (o.ghost && !RM))
    if (busy || dirty || (!RM && now - lastDraw > 66)) { draw(); placeBtns(); lastDraw = now; dirty = false }
    if (busy || !RM) kick()
  }
  const onMotion = (e: MediaQueryListEvent) => {
    RM = e.matches
    if (RM) {
      // Switched on mid-game: settle what is already moving (the next frame finishes it) and retire ghosts; fades stay.
      for (const tw of tweens) if (Object.keys(tw.to).some(k => k !== 'alpha')) tw.dur = 0
      hopAnim?.finish(); hopAnim = null
      for (const it of S.items) {
        if (it.fold) it.fold.t0 = now - FOLD_MS
        if (it.cloth) it.cloth.t0 = now - CLOTH_MS
        if (it.ghost) retireGhost(it)
      }
    }
    markDirty()
  }
  mqRM.addEventListener('change', onMotion)
  cleanups.push(() => mqRM.removeEventListener('change', onMotion))
  // The dashboard switches themes by changing attributes on <html>; re-read the colours when it does.
  const themeObs = new MutationObserver(() => { readTheme(); markDirty() })
  themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class', 'style'] })
  cleanups.push(() => themeObs.disconnect())
  const ro = new ResizeObserver(() => layout())
  ro.observe(root)
  cleanups.push(() => ro.disconnect())

  readTheme()
  layout()
  setCount(0)
  fillBasket()
  seedPile()
  S.started = true
  kick()

  return {
    restart,
    // Then lay out again: a longer language can wrap the counter row onto two lines, and the scene sits below it.
    retext() { setCount(S.count); for (const it of S.items) relabel(it); layout() },
    stop() {
      if (stopped) return
      stopped = true
      if (raf) window.cancelAnimationFrame(raf)
      raf = 0
      window.clearTimeout(sayTimer)
      for (const c of cleanups.splice(0)) c()
      tweens.length = 0
      timers.length = 0
      keysEl.textContent = ''
    },
  }
}
