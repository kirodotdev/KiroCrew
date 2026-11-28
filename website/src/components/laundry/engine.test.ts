import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { startLaundry, runTweenList, type LaundryText } from './engine'
import { GHOST_MARK, foldLayers, area, type Layer } from './geometry'
import engineSource from './engine.ts?raw'
import geometrySource from './geometry.ts?raw'
import waitGameSource from './WaitGame.tsx?raw'

// A frame queue the test drives by hand, and a clock it moves, so nothing waits on real time.
let queue: FrameRequestCallback[] = []
let clock = 1000
function frames(n = 40, step = 20) {
  for (let i = 0; i < n && queue.length; i++) {
    clock += step
    const run = queue
    queue = []
    for (const cb of run) cb(clock)
  }
}
function stubMotion(reduce: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: reduce && q.includes('reduced-motion'), media: q, addEventListener: () => {}, removeEventListener: () => {} }))
}

const TEXT: LaundryText = {
  folded: n => `F${n}`, item_basket: 'BASKET', item_table: 'TABLE', item_part_folded: 'PART', item_folded: 'FOLDED',
  placed_table: 'p-table', placed_basket: 'p-basket', placed_pile: 'p-pile', fold_done: 'done', fold_step: 'step',
  fold_first: 'first', table_full: 'full', picked_up: 'up', put_back: 'back', over_basket: 'o-b', over_table: 'o-t',
  over_pile: 'o-p', pile: 'PILE', ghost: 'GHOST', pile_full: 'pile-full', started_over: 'again',
}

function mount() {
  const root = document.createElement('div')
  const part = (tag: string, cls?: string) => { const el = document.createElement(tag); if (cls) el.className = cls; return el }
  root.append(part('canvas'), part('div', 'laundry-keys'), ...['count', 'hud', 'hint', 'toast', 'caption', 'live'].map(c => part('p', `laundry-${c}`)))
  document.body.appendChild(root)
  return root
}

/** Seeded random (mulberry32), so each run plays the same game. */
function seeded(seed: number) {
  let a = seed >>> 0
  return () => { a = (a + 0x6d2b79f5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296 }
}

function press(b: Element, key: string) { b.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })); frames(5) }
function button(root: HTMLElement, ...labels: string[]) {
  return [...root.querySelectorAll('.laundry-keys button')].find(b => labels.includes(b.getAttribute('aria-label') || ''))
}
/** Under reduced motion every move lands at once: basket -> table, fold, pick up, over the pile, drop. */
function stackOne(root: HTMLElement) {
  press(button(root, 'BASKET')!, 'Enter')
  for (let i = 0; i < 4; i++) { const t = button(root, 'TABLE', 'PART'); if (!t) break; press(t, 'Enter') }
  const f = button(root, 'FOLDED')!
  press(f, ' ')
  press(f, ' ')
  frames(20, 50)
}

beforeEach(() => {
  queue = []
  clock = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => clock)
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { queue.push(cb); return queue.length })
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.replaceChildren()
})

describe('laundry engine', () => {
  it('requests no frames once an idle scene has drawn under reduced motion', () => {
    stubMotion(true)
    const h = startLaundry(mount(), { text: () => TEXT, random: seeded(1) })
    frames()
    expect(queue).toHaveLength(0)
    h.stop()
  })

  it('keeps a frame loop for the idle bob when motion is allowed', () => {
    stubMotion(false)
    const h = startLaundry(mount(), { text: () => TEXT, random: seeded(1) })
    frames()
    expect(queue).toHaveLength(1)
    h.stop()
  })

  it('stop() cancels the frame and removes every window listener it added', () => {
    stubMotion(false)
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    const root = mount()
    const h = startLaundry(root, { text: () => TEXT, random: seeded(2) })
    frames(3)
    h.stop()
    const added = add.mock.calls.map(c => c[1])
    expect(added.length).toBeGreaterThan(0)
    for (const fn of added) expect(remove.mock.calls.some(c => c[1] === fn)).toBe(true)
    expect(window.cancelAnimationFrame).toHaveBeenCalled()
    expect(root.querySelectorAll('.laundry-keys button')).toHaveLength(0)
  })

  it('folds, stacks and counts by keyboard, and the first ghost comes on the first or second stack', () => {
    stubMotion(true)
    for (const seed of [3, 4, 5, 6, 7, 8]) {
      let ghosts = 0
      const root = mount()
      const text = () => ({ ...TEXT, get ghost() { ghosts++; return 'GHOST' } })
      const h = startLaundry(root, { text, random: seeded(seed) })
      frames()
      stackOne(root)
      expect(root.querySelector('.laundry-count')?.textContent).toBe('F2')
      // The count is its own visible text, not a drawn stack beside a bare number.
      expect(root.querySelector('.laundry-count')?.children).toHaveLength(0)
      stackOne(root)
      expect(root.querySelector('.laundry-count')?.textContent).toBe('F3')
      expect(ghosts).toBeGreaterThanOrEqual(1)
      h.stop()
      root.remove()
    }
  })

  it('R on the game starts over, even when the host stops keys from bubbling past it', () => {
    stubMotion(true)
    const host = document.createElement('div')
    host.addEventListener('keydown', e => e.stopPropagation())
    const root = mount()
    host.append(root)
    document.body.append(host)
    let again = 0
    const h = startLaundry(root, { text: () => ({ ...TEXT, get started_over() { again++; return 'again' } }), random: seeded(11) })
    frames()
    stackOne(root)
    press(button(root, 'BASKET')!, 'r')
    expect(again).toBe(1)
    expect(root.querySelector('.laundry-count')?.textContent).toBe('F1') // back to the seeded sheet alone
    h.stop()
  })

  it('a small embed still picks an item up from a tap just outside its drawing', () => {
    stubMotion(true)
    const root = mount()
    // A card about 224 x 200 px, as on a 320 px phone: the scene draws at about a quarter scale.
    root.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, width: 224, height: 200, right: 224, bottom: 200, toJSON: () => ({}) })
    const canvas = root.querySelector('canvas')!
    canvas.getBoundingClientRect = root.getBoundingClientRect
    const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
    frames()
    const btn = button(root, 'BASKET') as HTMLElement
    const left = parseFloat(btn.style.transform.match(/translate\(([-\d.]+)px/)![1]), top = parseFloat(btn.style.transform.match(/,([-\d.]+)px\)/)![1])
    const w = parseFloat(btn.style.width), hh = parseFloat(btn.style.height)
    // 6 px outside the item's box on the left, level with its middle: still picks it up.
    const at = { clientX: left - 6, clientY: top + hh / 2, pointerId: 1, pointerType: 'touch', button: 0, bubbles: true, cancelable: true }
    canvas.dispatchEvent(new PointerEvent('pointerdown', at))
    canvas.dispatchEvent(new PointerEvent('pointerup', at))
    frames()
    expect(button(root, 'TABLE')).toBeTruthy()
    expect(w).toBeGreaterThan(0)
    h.stop()
  })

  it('a screen reader\'s plain click folds an item and then puts it on the pile', () => {
    stubMotion(true)
    const root = mount()
    const h = startLaundry(root, { text: () => TEXT, random: seeded(13) })
    frames()
    const click = (b: Element) => { b.dispatchEvent(new MouseEvent('click', { detail: 0, bubbles: true })); frames(5) }
    clock += 1000 // no key pressed in the last 300 ms
    click(button(root, 'BASKET')!)
    for (let i = 0; i < 4; i++) { const t = button(root, 'TABLE', 'PART'); if (!t) break; click(t) }
    click(button(root, 'FOLDED')!)
    expect(root.querySelector('.laundry-count')?.textContent).toBe('F2')
    h.stop()
  })

  it('makes no request and touches no storage while it plays', () => {
    stubMotion(true)
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    const open = vi.spyOn(XMLHttpRequest.prototype, 'open')
    const ws = vi.fn()
    vi.stubGlobal('WebSocket', ws)
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const root = mount()
    const h = startLaundry(root, { text: () => TEXT, random: seeded(9) })
    frames()
    stackOne(root)
    h.restart()
    frames()
    h.stop()
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(open).not.toHaveBeenCalled()
    expect(ws).not.toHaveBeenCalled()
    expect(setItem).not.toHaveBeenCalled()
    // And none of the game's modules names a network or storage API, or a URL other than the SVG namespace.
    for (const src of [engineSource, geometrySource, waitGameSource]) {
      expect(src).not.toMatch(/\bfetch\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon|localStorage|sessionStorage|indexedDB|import\(/)
      expect(src.replace(/http:\/\/www\.w3\.org\/2000\/svg/g, '')).not.toMatch(/https?:\/\//)
    }
  })
})

describe('laundry ghost retirement under reduced motion', () => {
  /** A canvas context that counts the ellipses each draw makes: the basket's handles are always two, a ghost's eyes add more. */
  function recordEllipses() {
    const perDraw: number[] = []
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => new Proxy({}, {
      get: (_t, p) => (p === 'clearRect' ? () => { perDraw.push(0) } : p === 'ellipse' ? () => { perDraw[perDraw.length - 1]++ } : p === 'measureText' ? () => ({ width: 1 }) : () => {}),
      set: () => true,
    })) as never)
    return perDraw
  }
  /** Like `stackOne`, but each move gets the frames a real animation needs. */
  function playOne(root: HTMLElement) {
    const go = (b: Element | undefined, key: string) => { b!.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })); frames(60, 50) }
    go(button(root, 'BASKET'), 'Enter')
    for (let i = 0; i < 4; i++) { const t = button(root, 'TABLE', 'PART'); if (!t) break; go(t, 'Enter') }
    const f = button(root, 'FOLDED')
    go(f, ' '); go(f, ' ')
  }
  const lastDraw = (perDraw: number[]) => perDraw[perDraw.length - 1]

  it('retires a ghost that is already flying when the system turns reduced motion on', () => {
    let onChange: ((e: { matches: boolean }) => void) | undefined
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener: (_t: string, fn: typeof onChange) => { if (q.includes('reduced-motion')) onChange = fn }, removeEventListener: () => {} }))
    const perDraw = recordEllipses()
    let ghosts = 0
    const root = mount()
    const h = startLaundry(root, { text: () => ({ ...TEXT, get ghost() { ghosts++; return 'GHOST' } }), random: seeded(3), forceGhost: true })
    frames(60, 50)
    const bare = lastDraw(perDraw)
    for (let i = 0; i < 4 && !ghosts; i++) playOne(root)
    expect(ghosts).toBeGreaterThanOrEqual(1)
    frames(60, 50) // the cloth drapes and the ghost takes off
    expect(lastDraw(perDraw)).toBeGreaterThan(bare) // the ghost's eyes are on screen
    onChange!({ matches: true })
    frames(80, 50) // the retire timer, the fade, and the idle loop
    expect(lastDraw(perDraw)).toBe(bare)
    expect(queue).toHaveLength(0)
    h.stop()
  })

  it('settles a fold already under way when the system turns reduced motion on', () => {
    let onChange: ((e: { matches: boolean }) => void) | undefined
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener: (_t: string, fn: typeof onChange) => { if (q.includes('reduced-motion')) onChange = fn }, removeEventListener: () => {} }))
    recordEllipses()
    const root = mount()
    const h = startLaundry(root, { text: () => TEXT, random: seeded(3) })
    frames(60, 50)
    button(root, 'BASKET')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    frames(12, 50)
    button(root, 'TABLE')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    frames(2, 50) // 100 ms of a 480 ms fold
    expect(button(root, 'TABLE')).toBeDefined()
    onChange!({ matches: true })
    frames(2, 50)
    expect(button(root, 'TABLE')).toBeUndefined() // the fold step finished
    h.stop()
  })

  it('finishes the counter\'s hop when the system turns reduced motion on mid-hop', () => {
    let onChange: ((e: { matches: boolean }) => void) | undefined
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener: (_t: string, fn: typeof onChange) => { if (q.includes('reduced-motion')) onChange = fn }, removeEventListener: () => {} }))
    const finish = vi.fn()
    const animate = vi.fn(() => ({ finish }) as unknown as Animation)
    Object.defineProperty(HTMLElement.prototype, 'animate', { value: animate, configurable: true, writable: true })
    try {
      recordEllipses()
      const root = mount()
      const h = startLaundry(root, { text: () => TEXT, random: seeded(3) })
      frames(60, 50)
      // with motion on, each move and fold takes frames to finish
      press(button(root, 'BASKET')!, 'Enter'); frames(20, 50)
      for (let i = 0; i < 6 && !button(root, 'FOLDED'); i++) { const t = button(root, 'TABLE', 'PART'); if (!t) break; press(t, 'Enter'); frames(12, 50) }
      const f = button(root, 'FOLDED')!
      press(f, ' '); press(f, ' '); frames(20, 50)
      expect(animate).toHaveBeenCalled()
      onChange!({ matches: true })
      expect(finish).toHaveBeenCalledTimes(1)
      h.stop()
    } finally { delete (HTMLElement.prototype as { animate?: unknown }).animate }
  })

  it('still retires a ghost born under reduced motion', () => {
    stubMotion(true)
    const perDraw = recordEllipses()
    let ghosts = 0
    const root = mount()
    const h = startLaundry(root, { text: () => ({ ...TEXT, get ghost() { ghosts++; return 'GHOST' } }), random: seeded(3), forceGhost: true })
    frames()
    const bare = lastDraw(perDraw)
    for (let i = 0; i < 4 && !ghosts; i++) stackOne(root)
    expect(ghosts).toBeGreaterThanOrEqual(1)
    frames(80, 50)
    expect(lastDraw(perDraw)).toBe(bare)
    expect(queue).toHaveLength(0)
    h.stop()
  })
})

describe('laundry drag cancel', () => {
  /** A drag from the basket to the middle of a 224 x 200 card, with Escape pressed inside `host`, then the pointer released. */
  function dragAndEscape(host: HTMLElement, root: HTMLElement, escape = true) {
    const canvas = root.querySelector('canvas')!
    const btn = button(root, 'BASKET') as HTMLElement
    const left = parseFloat(btn.style.transform.match(/translate\(([-\d.]+)px/)![1]), top = parseFloat(btn.style.transform.match(/,([-\d.]+)px\)/)![1])
    const hh = parseFloat(btn.style.height), ww = parseFloat(btn.style.width)
    const at = (x: number, y: number) => ({ clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', button: 0, bubbles: true, cancelable: true })
    canvas.dispatchEvent(new PointerEvent('pointerdown', at(left + ww / 2, top + hh / 2)))
    canvas.dispatchEvent(new PointerEvent('pointermove', at(left + ww / 2 + 20, top + hh / 2 - 30)))
    canvas.dispatchEvent(new PointerEvent('pointermove', at(112, 100))) // the middle of the card: the table
    if (escape) root.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    canvas.dispatchEvent(new PointerEvent('pointerup', at(112, 100)))
    frames()
    expect(host.contains(root)).toBe(true)
  }
  function mountCard() {
    stubMotion(true)
    const host = document.createElement('div')
    const root = mount()
    host.appendChild(root)
    document.body.appendChild(host)
    root.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, width: 224, height: 200, right: 224, bottom: 200, toJSON: () => ({}) })
    root.querySelector('canvas')!.getBoundingClientRect = root.getBoundingClientRect
    return { host, root }
  }

  it('Escape returns a dragged item to its basket slot even where an ancestor stops key events, as the update overlay does', () => {
    const { host, root } = mountCard()
    host.addEventListener('keydown', e => e.stopPropagation()) // mirrors isolateOverlayKeys at the overlay's root
    const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
    frames()
    dragAndEscape(host, root)
    expect(button(root, 'BASKET')).toBeTruthy()
    expect(button(root, 'TABLE')).toBeFalsy()
    h.stop()
  })

  it('without Escape, the same drag drops the item on the table (the control for the test above)', () => {
    const { host, root } = mountCard()
    const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
    frames()
    dragAndEscape(host, root, false)
    expect(button(root, 'TABLE')).toBeTruthy()
    h.stop()
  })

  it('stops listening for Escape once disposed', () => {
    const { host, root } = mountCard()
    const add = vi.spyOn(window, 'addEventListener'), remove = vi.spyOn(window, 'removeEventListener')
    const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
    frames()
    const key = add.mock.calls.filter(c => c[0] === 'keydown')
    expect(key.length).toBeGreaterThan(0)
    h.stop()
    for (const [type, fn, opts] of key) {
      expect(remove.mock.calls.some(c => c[0] === type && c[1] === fn && (typeof c[2] === 'object' ? c[2]?.capture : c[2]) === (typeof opts === 'object' ? opts?.capture : opts))).toBe(true)
    }
    host.remove()
  })
})

describe('laundry scene chrome', () => {
  function recorder() {
    const texts: string[] = [], transforms: number[][] = []
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => new Proxy({}, {
      get: (_t, p) => (p === 'fillText' ? (t: string) => { texts.push(t) } : p === 'setTransform' ? (...a: number[]) => { transforms.push(a) } : p === 'measureText' ? () => ({ width: 1 }) : () => {}),
      set: () => true,
    })) as never)
    return { texts, transforms }
  }
  function card() {
    stubMotion(true)
    const root = mount()
    root.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, width: 224, height: 200, right: 224, bottom: 200, toJSON: () => ({}) })
    root.querySelector('canvas')!.getBoundingClientRect = root.getBoundingClientRect
    return root
  }

  it('names the pile on the canvas, so "drag to the pile" has a target a person recognises', () => {
    const { texts } = recorder()
    const h = startLaundry(card(), { text: () => TEXT, random: seeded(12) })
    frames()
    expect(texts).toContain('PILE')
    h.stop()
  })

  it('draws the Pile word on the tabletop beside the stack, not under the table', () => {
    const texts: { t: string; x: number; y: number }[] = [], transforms: number[][] = []
    let font = ''
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => new Proxy({}, {
      get: (_t, p) => (p === 'fillText' ? (t: string, x: number, y: number) => { texts.push({ t, x, y }) } : p === 'setTransform' ? (...a: number[]) => { transforms.push(a) } : p === 'measureText' ? (t: string) => ({ width: t.length * 0.6 * parseFloat(font) }) : p === 'font' ? font : () => {}),
      set: (_t, p, v) => { if (p === 'font') font = v; return true },
    })) as never)
    const root = card()
    const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
    frames()
    const lab = texts.filter(x => x.t === 'PILE').pop()!
    const sc = transforms.filter(t => t[0] !== 1).pop()![3]
    const portrait = root.dataset.layout === 'portrait'
    const top = portrait ? 390 : 300, px = portrait ? 326 : 800 // the layout's tabletop and pile centre (engine.ts LAND / PORT)
    const size = Math.max(14, 11 / sc), w = 'PILE'.length * 0.6 * size
    expect(lab.y + size).toBeLessThanOrEqual(top) // the word's bottom is on or above the tabletop
    // beside the stack, clear of the pile's 62-unit target, or (no room beside it) over the stack inside that target:
    // never across its edge onto the table, where a drop meant for the table would land on the pile
    const d = Math.abs(lab.x - px)
    expect(d - w / 2 >= 62 || d + w / 2 <= 62).toBe(true)
    h.stop()
  })

  // Above the stack, a word wider than the pile column (German at the font floor on a narrow card) overhangs the table;
  // the table under the overhang stays table, and the word's middle, over the column, stays pile.
  function narrowWide() {
    const texts: { t: string; x: number; y: number }[] = [], transforms: number[][] = []
    let font = ''
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => new Proxy({}, {
      get: (_t, p) => (p === 'fillText' ? (t: string, x: number, y: number) => { texts.push({ t, x, y }) } : p === 'setTransform' ? (...a: number[]) => { transforms.push(a) } : p === 'measureText' ? (t: string) => ({ width: t.length * 0.6 * parseFloat(font) }) : p === 'font' ? font : () => {}),
      set: (_t, p, v) => { if (p === 'font') font = v; return true },
    })) as never)
    stubMotion(true)
    const root = mount()
    root.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, width: 470, height: 400, right: 470, bottom: 400, toJSON: () => ({}) })
    root.querySelector('canvas')!.getBoundingClientRect = root.getBoundingClientRect
    let firsts = 0
    const WORD = 'STAPELSTAPEL'
    const h = startLaundry(root, { text: () => ({ ...TEXT, pile: WORD, get fold_first() { firsts++; return 'first' } }), random: seeded(12) })
    frames()
    const lab = texts.filter(x => x.t === WORD).pop()!
    const [, , , sc, ox, oy] = transforms.filter(t => t[0] !== 1).pop()!
    const size = Math.max(14, 11 / sc), w = WORD.length * 0.6 * size
    const at = (x: number, y: number) => ({ clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', button: 0, bubbles: true, cancelable: true })
    const dragTo = (b: HTMLElement, wx: number, wy: number) => {
      const m = b.style.transform.match(/translate\(([-\d.]+)px,\s*([-\d.]+)px\)/)!
      const bx = parseFloat(m[1]) + parseFloat(b.style.width) / 2, by = parseFloat(m[2]) + parseFloat(b.style.height) / 2
      const cv = root.querySelector('canvas')!, tx = ox + wx * sc, ty = oy + wy * sc
      cv.dispatchEvent(new PointerEvent('pointerdown', at(bx, by)))
      cv.dispatchEvent(new PointerEvent('pointermove', at(bx + 10, by - 10)))
      cv.dispatchEvent(new PointerEvent('pointermove', at(tx, ty)))
      cv.dispatchEvent(new PointerEvent('pointerup', at(tx, ty)))
      frames(20, 50)
    }
    return { root, h, lab, size, w, dragTo, firsts: () => firsts }
  }
  it('treats the table under an above-the-stack Pile word that overhangs the pile column as table', () => {
    const { root, h, lab, size, w, dragTo, firsts } = narrowWide()
    expect(w / 2).toBeGreaterThan(62) // the word overhangs the 124-unit column
    expect(Math.abs(lab.x - 800)).toBeLessThan(1) // placed above the stack
    dragTo(button(root, 'BASKET') as HTMLElement, lab.x + w / 2 - 4, lab.y + size / 2) // the overhanging end, over the table
    expect(firsts()).toBe(0) // not a drop on the pile ("Fold it first.")
    expect(button(root, 'TABLE')).toBeTruthy()
    h.stop()
  })
  it('still counts a drop on the middle of an above-the-stack Pile word as a drop on the pile', () => {
    const { root, h, lab, size, dragTo, firsts } = narrowWide()
    dragTo(button(root, 'BASKET') as HTMLElement, lab.x, lab.y + size / 2)
    expect(firsts()).toBeGreaterThan(0) // the pile turned the unfolded item away ("Fold it first." said and shown): a pile drop
    h.stop()
  })

  it('keeps the Pile word off the table in a scene too narrow for it beside the stack', () => {
    const texts: { t: string; x: number; y: number }[] = [], transforms: number[][] = []
    let font = ''
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => new Proxy({}, {
      get: (_t, p) => (p === 'fillText' ? (t: string, x: number, y: number) => { texts.push({ t, x, y }) } : p === 'setTransform' ? (...a: number[]) => { transforms.push(a) } : p === 'measureText' ? (t: string) => ({ width: t.length * 0.6 * parseFloat(font) }) : p === 'font' ? font : () => {}),
      set: (_t, p, v) => { if (p === 'font') font = v; return true },
    })) as never)
    stubMotion(true)
    const root = mount()
    // a pane whose width, not its height, sets the scale: the scene fills it edge to edge, so nothing is right of the pile
    root.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, width: 470, height: 400, right: 470, bottom: 400, toJSON: () => ({}) })
    root.querySelector('canvas')!.getBoundingClientRect = root.getBoundingClientRect
    const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
    frames()
    const lab = texts.filter(x => x.t === 'PILE').pop()!
    const sc = transforms.filter(t => t[0] !== 1).pop()![3]
    const size = Math.max(14, 11 / sc), w = 'PILE'.length * 0.6 * size
    expect(root.dataset.layout).toBe('landscape')
    expect(Math.abs(lab.x - 800) + w / 2).toBeLessThanOrEqual(62) // over the stack, inside the pile's own target
    h.stop()
  })

  it('counts a drop on the Pile word as a drop on the pile', () => {
    const texts: { t: string; x: number; y: number }[] = [], transforms: number[][] = []
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => new Proxy({}, {
      get: (_t, p) => (p === 'fillText' ? (t: string, x: number, y: number) => { texts.push({ t, x, y }) } : p === 'setTransform' ? (...a: number[]) => { transforms.push(a) } : p === 'measureText' ? () => ({ width: 1 }) : () => {}),
      set: () => true,
    })) as never)
    const root = card()
    const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
    frames()
    press(button(root, 'BASKET')!, 'Enter')
    for (let i = 0; i < 4 && !button(root, 'FOLDED'); i++) { const t = button(root, 'TABLE', 'PART'); if (!t) break; press(t, 'Enter') }
    frames(20, 50)
    const f = button(root, 'FOLDED') as HTMLElement
    expect(f).toBeTruthy()
    const [, , , sc, ox, oy] = transforms.filter(t => t[0] !== 1).pop()!
    const lab = texts.filter(x => x.t === 'PILE').pop()!
    const at = (x: number, y: number) => ({ clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', button: 0, bubbles: true, cancelable: true })
    const m = f.style.transform.match(/translate\(([-\d.]+)px,\s*([-\d.]+)px\)/)!
    const fx = parseFloat(m[1]) + parseFloat(f.style.width) / 2, fy = parseFloat(m[2]) + parseFloat(f.style.height) / 2
    const cv = root.querySelector('canvas')!
    cv.dispatchEvent(new PointerEvent('pointerdown', at(fx, fy)))
    cv.dispatchEvent(new PointerEvent('pointermove', at(fx + 10, fy - 10)))
    const lx = ox + lab.x * sc, ly = oy + (lab.y + Math.max(14, 11 / sc) / 2) * sc // textBaseline is top: the word's middle
    cv.dispatchEvent(new PointerEvent('pointermove', at(lx, ly)))
    cv.dispatchEvent(new PointerEvent('pointerup', at(lx, ly)))
    frames(20, 50)
    expect(root.querySelector('.laundry-count')!.textContent).toBe('F2') // the seeded sheet and this one
    h.stop()
  })

  it('counts a drop on the outer glyph of a full-width Pile word as a drop on the pile', () => {
    const texts: { t: string; x: number; y: number }[] = [], transforms: number[][] = []
    let font = ''
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => new Proxy({}, {
      // a full-width face: every glyph advances 1em, as CJK glyphs do
      get: (_t, p) => (p === 'fillText' ? (t: string, x: number, y: number) => { texts.push({ t, x, y }) } : p === 'setTransform' ? (...a: number[]) => { transforms.push(a) } : p === 'measureText' ? (t: string) => ({ width: [...t].length * parseFloat(font) }) : p === 'font' ? font : () => {}),
      set: (_t, p, v) => { if (p === 'font') font = v; return true },
    })) as never)
    const root = card()
    const h = startLaundry(root, { text: () => ({ ...TEXT, pile: '衣服堆' }), random: seeded(12) })
    frames()
    press(button(root, 'BASKET')!, 'Enter')
    for (let i = 0; i < 4 && !button(root, 'FOLDED'); i++) { const t = button(root, 'TABLE', 'PART'); if (!t) break; press(t, 'Enter') }
    frames(20, 50)
    const f = button(root, 'FOLDED') as HTMLElement
    expect(f).toBeTruthy()
    const [, , , sc, ox, oy] = transforms.filter(t => t[0] !== 1).pop()!
    const lab = texts.filter(x => x.t === '衣服堆').pop()!
    const size = Math.max(14, 11 / sc)
    const at = (x: number, y: number) => ({ clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', button: 0, bubbles: true, cancelable: true })
    const m = f.style.transform.match(/translate\(([-\d.]+)px,\s*([-\d.]+)px\)/)!
    const fx = parseFloat(m[1]) + parseFloat(f.style.width) / 2, fy = parseFloat(m[2]) + parseFloat(f.style.height) / 2
    const cv = root.querySelector('canvas')!
    cv.dispatchEvent(new PointerEvent('pointerdown', at(fx, fy)))
    cv.dispatchEvent(new PointerEvent('pointermove', at(fx + 10, fy - 10)))
    // the middle of the last glyph: 1.25em right of centre, inside the 3em word
    const lx = ox + (lab.x + 1.25 * size) * sc, ly = oy + (lab.y + size / 2) * sc
    cv.dispatchEvent(new PointerEvent('pointermove', at(lx, ly)))
    cv.dispatchEvent(new PointerEvent('pointerup', at(lx, ly)))
    frames(20, 50)
    expect(root.querySelector('.laundry-count')!.textContent).toBe('F2') // the seeded sheet and this one
    h.stop()
  })

  it('rewrites its counter and item labels in the new language when asked (a language switch)', () => {
    let text: LaundryText = TEXT
    const root = card()
    const h = startLaundry(root, { text: () => text, random: seeded(12) })
    frames()
    stackOne(root)
    expect(root.querySelector('.laundry-count')!.textContent).toBe('F2')
    text = { ...TEXT, folded: n => `S${n}`, item_basket: 'KORB' }
    h.retext()
    expect(root.querySelector('.laundry-count')!.textContent).toBe('S2')
    expect(button(root, 'KORB')).toBeTruthy()
    expect(button(root, 'BASKET')).toBeUndefined()
    h.stop()
  })

  it('consumes the Escape that cancels a drag, so a host\'s own Escape handler never sees it', () => {
    const root = card()
    const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
    frames()
    const hostEscape = vi.fn() // stands in for the update dialog's window listener, which closes and snoozes
    window.addEventListener('keydown', hostEscape)
    try {
      const b = button(root, 'BASKET') as HTMLElement
      const m = b.style.transform.match(/translate\(([-\d.]+)px,\s*([-\d.]+)px\)/)!
      const x = parseFloat(m[1]) + parseFloat(b.style.width) / 2, y = parseFloat(m[2]) + parseFloat(b.style.height) / 2
      const at = (cx: number, cy: number) => ({ clientX: cx, clientY: cy, pointerId: 1, pointerType: 'mouse', button: 0, bubbles: true, cancelable: true })
      const cv = root.querySelector('canvas')!
      cv.dispatchEvent(new PointerEvent('pointerdown', at(x, y)))
      cv.dispatchEvent(new PointerEvent('pointermove', at(x + 30, y - 30)))
      document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
      expect(hostEscape).not.toHaveBeenCalled()
      frames(20, 50)
      expect(button(root, 'BASKET')).toBeTruthy() // the item went back
      // no drag: Escape is the host's
      document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
      expect(hostEscape).toHaveBeenCalledTimes(1)
    } finally { window.removeEventListener('keydown', hostEscape); h.stop() }
  })

  it('lays the scene out again under a taller counter row after a language switch', () => {
    const transforms: number[][] = []
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => new Proxy({}, {
      get: (_t, p) => (p === 'setTransform' ? (...a: number[]) => { transforms.push(a) } : p === 'measureText' ? () => ({ width: 1 }) : () => {}),
      set: () => true,
    })) as never)
    const root = card()
    const hud = root.querySelector('.laundry-hud') as HTMLElement
    let hudPx = 20
    Object.defineProperty(hud, 'offsetHeight', { get: () => hudPx, configurable: true })
    const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
    frames()
    const before = transforms.filter(t => t[0] !== 1).pop()!
    hudPx = 60 // the counter and button wrapped onto two rows in the new language
    h.retext()
    frames()
    const after = transforms.filter(t => t[0] !== 1).pop()!
    expect(after[5]).toBeGreaterThan(before[5]) // the scene starts lower, under the taller row
    expect(after[3]).toBeLessThan(before[3]) // and is drawn smaller to fit what is left
    h.stop()
  })

  // The basket holds five: a replacement drops in 450 ms after an item leaves it, so a return after that must not make six.
  const basketButtons = (root: HTMLElement) => [...root.querySelectorAll('.laundry-keys button')].filter(b => b.getAttribute('aria-label') === 'BASKET') as HTMLElement[]
  // a basket slot is a fixed x (five, about 10 px apart in this card); the button's box centres on its item
  const uniqueSpots = (bs: HTMLElement[]) => new Set(bs.map(b => {
    const m = b.style.transform.match(/translate\(([-\d.]+)px/)!
    return Math.round((parseFloat(m[1]) + parseFloat(b.style.width) / 2) / 4)
  })).size
  function returnByKeyboard(root: HTMLElement) {
    const t = button(root, 'TABLE')!
    press(t, ' ') // picked up, aimed at the next spot
    press(t, 'ArrowLeft'); press(t, 'ArrowLeft') // back to the basket
    press(t, ' ')
  }
  function returnByPointer(root: HTMLElement) {
    const centre = (b: Element) => { const el = b as HTMLElement, m = el.style.transform.match(/translate\(([-\d.]+)px,\s*([-\d.]+)px\)/)!; return [parseFloat(m[1]) + parseFloat(el.style.width) / 2, parseFloat(m[2]) + parseFloat(el.style.height) / 2] }
    const at = (x: number, y: number) => ({ clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', button: 0, bubbles: true, cancelable: true })
    const [tx, ty] = centre(button(root, 'TABLE')!), [bx, by] = centre(basketButtons(root)[0])
    const cv = root.querySelector('canvas')!
    cv.dispatchEvent(new PointerEvent('pointerdown', at(tx, ty)))
    cv.dispatchEvent(new PointerEvent('pointermove', at(tx - 20, ty)))
    cv.dispatchEvent(new PointerEvent('pointermove', at(bx, by)))
    cv.dispatchEvent(new PointerEvent('pointerup', at(bx, by)))
    frames(10, 50)
  }
  for (const [how, back] of [['pointer', returnByPointer], ['keyboard', returnByKeyboard]] as const) {
    it(`keeps five items on five spots when one is returned (${how}) after its replacement dropped in, even ten times over`, () => {
      const root = card()
      const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
      frames()
      for (let round = 0; round < 10; round++) {
        if (!button(root, 'TABLE')) { press(basketButtons(root)[0], 'Enter'); frames(30, 50) } // to the table; the replacement drops in
        expect(basketButtons(root)).toHaveLength(5)
        back(root)
        frames(30, 50)
        const bs = basketButtons(root)
        expect(bs).toHaveLength(5) // still five focus buttons in the basket
        expect(uniqueSpots(bs)).toBe(5) // on five different spots
        expect(button(root, 'TABLE')).toBeTruthy() // the returned item went to the table instead
      }
      h.stop()
    })
  }

  // An item in the hand keeps its basket slot: a replacement that drops in meanwhile must not take it, or a cancel puts
  // two items on one spot. Several seeds, so the newcomer's pick among the free slots lands on the held one.
  const SEEDS = [1, 2, 3, 4, 5, 6, 7, 8]
  const cancels: [string, (root: HTMLElement, held: HTMLElement) => void][] = [
    ['Escape', (_r, held) => press(held, 'Escape')],
    ['blur (Tab away)', (_r, held) => { held.dispatchEvent(new FocusEvent('blur')); frames(5) }],
  ]
  for (const [name, cancel] of cancels) {
    it(`keeps a held basket item's slot for it through a replacement drop-in, cancelled by ${name}`, () => {
      for (const seed of SEEDS) {
        const root = card()
        const h = startLaundry(root, { text: () => TEXT, random: seeded(seed) })
        frames()
        press(basketButtons(root)[0], 'Enter') // to the table: a replacement is queued for 450 ms on
        const held = basketButtons(root)[0]
        held.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true })) // picked up before it fires
        frames(30, 50) // the replacement drops in
        cancel(root, held)
        frames(30, 50)
        const bs = basketButtons(root)
        expect(bs).toHaveLength(5)
        expect(uniqueSpots(bs)).toBe(5)
        h.stop(); root.remove()
      }
    })
  }
  it('returns an item to the table, without looping, when both the basket and the table are full', () => {
    const root = card()
    let fulls = 0
    const h = startLaundry(root, { text: () => ({ ...TEXT, get table_full() { fulls++; return 'full' } }), random: seeded(12) })
    frames()
    for (let i = 0; i < 12 && !fulls; i++) { press(basketButtons(root)[0], 'Enter'); frames(30, 50) }
    expect(fulls).toBeGreaterThan(0) // the table is full
    frames(30, 50)
    expect(basketButtons(root)).toHaveLength(5)
    const onTable = root.querySelectorAll('.laundry-keys button[aria-label="TABLE"]').length
    fulls = 0
    returnByKeyboard(root) // a full basket refuses it, and the full table takes it back without bouncing it again
    frames(30, 50)
    expect(basketButtons(root)).toHaveLength(5)
    expect(root.querySelectorAll('.laundry-keys button[aria-label="TABLE"]')).toHaveLength(onTable)
    expect(fulls).toBe(0) // no table-full round trip: one call, no recursion
    h.stop()
  })

  it('reallocates the canvas when the display scale changes with no resize, even with no frame running', () => {
    const resolution: { q: string; fn: () => void }[] = []
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q.includes('reduced-motion'), media: q, // reduced motion: an idle scene runs no frames
      addEventListener: (_t: string, fn: () => void) => { if (q.includes('resolution')) resolution.push({ q, fn }) },
      removeEventListener: (_t: string, fn: () => void) => { const k = resolution.findIndex(r => r.fn === fn && r.q === q); if (k >= 0) resolution.splice(k, 1) },
    }))
    const root = mount()
    root.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, width: 224, height: 200, right: 224, bottom: 200, toJSON: () => ({}) })
    const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
    frames()
    const cv = root.querySelector('canvas')!
    const w1 = cv.width
    const was = window.devicePixelRatio
    Object.defineProperty(window, 'devicePixelRatio', { value: (was || 1) * 2, configurable: true })
    try {
      expect(resolution).toHaveLength(1)
      resolution[0].fn() // the window moved to a screen at twice the scale: the old ratio's query stops matching
      expect(cv.width).toBe(w1 * 2) // no frame ran
      expect(resolution[0].q).toContain(`${(was || 1) * 2}dppx`) // and it now watches the new ratio
    } finally { Object.defineProperty(window, 'devicePixelRatio', { value: was, configurable: true }); h.stop() }
    expect(resolution).toHaveLength(0) // stop() removed it
  })

  it('drops a dragged item where it is released, even when the release comes before the next frame', () => {
    const transforms: number[][] = []
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => new Proxy({}, {
      get: (_t, p) => (p === 'setTransform' ? (...a: number[]) => { transforms.push(a) } : p === 'measureText' ? () => ({ width: 1 }) : () => {}),
      set: () => true,
    })) as never)
    const root = card()
    const h = startLaundry(root, { text: () => TEXT, random: seeded(12) })
    frames()
    const at = (x: number, y: number) => ({ clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', button: 0, bubbles: true, cancelable: true })
    const centre = (b: Element) => { const el = b as HTMLElement, m = el.style.transform.match(/translate\(([-\d.]+)px,\s*([-\d.]+)px\)/)!; return [parseFloat(m[1]) + parseFloat(el.style.width) / 2, parseFloat(m[2]) + parseFloat(el.style.height) / 2] }
    const b = button(root, 'BASKET')!
    const [bx, by] = centre(b)
    const [, , , sc, ox, oy] = transforms.filter(t => t[0] !== 1).pop()!
    const portrait = root.dataset.layout === 'portrait'
    // two spots over the table, far apart: the drag is drawn at the first, then released at the second in the same frame
    const sx = (wx: number) => ox + wx * sc, sy = oy + ((portrait ? 390 : 300) - 60) * sc
    const first = portrait ? 60 : 380, second = portrait ? 230 : 690
    const cv = root.querySelector('canvas')!
    cv.dispatchEvent(new PointerEvent('pointerdown', at(bx, by)))
    cv.dispatchEvent(new PointerEvent('pointermove', at(sx(first), sy)))
    frames(3, 20)
    cv.dispatchEvent(new PointerEvent('pointermove', at(sx(second), sy)))
    cv.dispatchEvent(new PointerEvent('pointerup', at(sx(second), sy)))
    frames(40, 50)
    const placed = button(root, 'TABLE')!
    const [px] = centre(placed)
    expect(Math.abs(px - sx(second))).toBeLessThan(Math.abs(px - sx(first)))
    h.stop()
  })

  it('anchors the scene just under the counter row, not centred in the pane', () => {
    const { transforms } = recorder()
    const h = startLaundry(card(), { text: () => TEXT, random: seeded(12) })
    frames()
    // setTransform(dpr*s, 0, 0, dpr*s, dpr*ox, dpr*oy): the last call is the scene's. The counter row is 0 px tall in jsdom, so 12 px of margin.
    const scene = transforms.filter(t => t[0] !== 1).pop()!
    expect(scene[5]).toBe(12)
    h.stop()
  })
})

describe('laundry tween loop', () => {
  const tw = (o: Record<string, number>, to: number, dur: number, done?: () => void) => ({ o, from: { x: o.x }, to: { x: to }, t0: 0, dur, ease: (t: number) => t, done })
  it('steps every tween in the frame, even when a finished tween\'s callback cancels an earlier one', () => {
    const early = { x: 0 }, a = { x: 0 }, b = { x: 0 }
    const list = [tw(early, 100, 1000), tw(a, 10, 100, () => { list.splice(0, 1) /* what a done callback's tween() does to an earlier tween on the same key */ }), tw(b, 10, 100)]
    runTweenList(list, 100)
    expect(a.x).toBe(10)
    expect(b.x).toBe(10) // not left for the next frame
    expect(list).toHaveLength(0)
  })

  it('runs a finished tween\'s callback after the frame\'s stepping, in order', () => {
    const calls: string[] = []
    const list = [tw({ x: 0 }, 1, 10, () => calls.push('first')), tw({ x: 0 }, 1, 10, () => calls.push('second'))]
    runTweenList(list, 10)
    expect(calls).toEqual(['first', 'second'])
  })
})

describe('laundry counter', () => {
  // The pile starts on a folded sheet: the count agrees with what the pile shows from the first frame.
  it('starts the count at the folded sheet already on the pile, and counts on from there', () => {
    stubMotion(true)
    const root = mount()
    const h = startLaundry(root, { text: () => TEXT, random: seeded(3) })
    frames()
    expect(root.querySelector('.laundry-count')!.textContent).toBe('F1')
    stackOne(root)
    expect(root.querySelector('.laundry-count')!.textContent).toBe('F2')
    h.stop()
  })
})

describe('laundry hint and layout', () => {
  // The how-to line no longer goes away after the first stack: it is the one hint line until the keyboard hint takes
  // its place (laundry.css), so a pointer player never sees neither.
  it('leaves the how-to line in place across the first stack', () => {
    stubMotion(true)
    const root = mount()
    const h = startLaundry(root, { text: () => TEXT, random: seeded(3) })
    frames()
    const before = root.className
    stackOne(root)
    expect(root.className).toBe(before)
    h.stop()
  })

  it('keeps the basket beside the table on a phone-width pane, and stacks it only when the pane is much taller than wide', () => {
    stubMotion(true)
    const layoutAt = (w: number, h: number) => {
      const root = mount()
      root.getBoundingClientRect = () => ({ width: w, height: h, left: 0, top: 0, right: w, bottom: h, x: 0, y: 0, toJSON: () => ({}) })
      const g = startLaundry(root, { text: () => TEXT, random: seeded(2) })
      frames()
      const out = root.dataset.layout
      g.stop()
      return out
    }
    expect(layoutAt(272, 358)).toBe('landscape')
    expect(layoutAt(240, 480)).toBe('portrait')
  })
})

describe('laundry geometry', () => {
  it('reads the ghost from the product mark: one body outline and two eyes on its right side', () => {
    expect(GHOST_MARK.body.length).toBeGreaterThan(50)
    // Two eyes, left then right: the ghost faces right, so the second eye sits further right.
    const [left, right] = GHOST_MARK.eyes
    expect(right[0]).toBeGreaterThan(left[0])
    for (const [x, y, rx, ry] of GHOST_MARK.eyes) {
      expect(x).toBeGreaterThan(0.5)
      expect(y).toBeGreaterThan(0.2)
      expect(y).toBeLessThan(0.6)
      expect(rx).toBeGreaterThan(0)
      expect(ry).toBeGreaterThan(rx)
    }
  })

  it('a fold turns the flap over the crease and keeps the area', () => {
    const towel: Layer[] = [{ pts: [[-60, -35], [60, -35], [60, 35], [-60, 35]], lines: [] }]
    const { stat, flap } = foldLayers(towel, { ax: 0, at: 20, dir: 1 }, -1)
    expect(area(stat[0].pts) + area(flap[0].pts)).toBeCloseTo(120 * 70)
    expect(Math.max(...flap[0].pts.map(p => p[0]))).toBeLessThanOrEqual(20)
  })
})
