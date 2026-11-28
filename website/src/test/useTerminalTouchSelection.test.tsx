import { describe, it, expect, vi, afterEach, beforeEach, onTestFinished } from 'vitest'
import { renderHook, act, cleanup, render, screen, fireEvent } from '@testing-library/react'
import type { Terminal } from '@xterm/xterm'
import {
  useTerminalTouchSelection,
  clientPointToCell,
  flingVelocity,
  TOUCH_HANDLE_ATTR,
} from '../hooks/useTerminalTouchSelection'
import TerminalKeyBar from '../components/TerminalKeyBar'

/**
 * The geometry is a pure function: xterm has no point→cell API, so the hook
 * derives the cell from `.xterm-screen`'s rect and the grid size.
 */
describe('clientPointToCell', () => {
  // 100px wide, 10 cols → 10px per col; 80px tall, 4 rows → 20px per row.
  const rect = { left: 0, top: 100, width: 100, height: 80 }

  it('maps a point to its column and absolute buffer row', () => {
    expect(clientPointToCell(35, 125, rect, 10, 4, 0)).toEqual({ col: 3, row: 1 })
    expect(clientPointToCell(35, 125, rect, 10, 4, 50)).toEqual({ col: 3, row: 51 })
  })

  it('clamps a point outside the grid to its edge cells', () => {
    expect(clientPointToCell(-5, -5, rect, 10, 4, 0)).toEqual({ col: 0, row: 0 })
    expect(clientPointToCell(999, 999, rect, 10, 4, 0)).toEqual({ col: 9, row: 3 })
  })

  it('returns null when geometry is unusable', () => {
    expect(clientPointToCell(5, 105, rect, 0, 4, 0)).toBeNull()
    expect(clientPointToCell(5, 105, { ...rect, height: 0 }, 10, 4, 0)).toBeNull()
  })
})

describe('flingVelocity', () => {
  it('measures the speed of the last 100ms of the drag', () => {
    // Finger moved up 100px in 50ms → 2 px/ms toward newer output.
    expect(flingVelocity([{ t: 0, y: 300 }, { t: 50, y: 200 }], 60)).toBeCloseTo(2)
  })

  it('is zero when the finger paused before lifting', () => {
    expect(flingVelocity([{ t: 0, y: 300 }, { t: 50, y: 200 }], 400)).toBe(0)
  })
})

/**
 * A fake terminal on REAL DOM: the hook binds native capture listeners on the
 * host, so the events are real TouchEvents dispatched at real elements. The
 * buffer is 10 columns; select()/getSelectionPosition() model xterm's linear
 * (row * cols + col) selection, and `.xterm-viewport` is a plain element whose
 * scrollTop the fling writes.
 */
const COLS = 10
const ROWS = 4
const CELL_W = 10
const CELL_H = 20

function makeTerm(lines: string[]) {
  const host = document.createElement('div')
  const element = document.createElement('div')
  element.className = 'xterm'
  const viewport = document.createElement('div')
  viewport.className = 'xterm-viewport'
  const screenEl = document.createElement('div')
  screenEl.className = 'xterm-screen'
  element.append(viewport, screenEl)
  host.append(element)
  document.body.append(host)
  Object.defineProperty(element, 'offsetParent', { get: () => document.body })
  const rect = (left: number, top: number, width: number, height: number) => () =>
    ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON() {} }) as DOMRect
  host.getBoundingClientRect = rect(0, 0, COLS * CELL_W, ROWS * CELL_H)
  screenEl.getBoundingClientRect = rect(0, 0, COLS * CELL_W, ROWS * CELL_H)

  let sel: { s: number; len: number } | null = null
  const selListeners = new Set<() => void>()
  const on = (set: Set<() => void>) => (cb: () => void) => { set.add(cb); return { dispose: () => set.delete(cb) } }
  const flat = lines.map(l => l.padEnd(COLS, ' ').slice(0, COLS)).join('')
  // Markers model xterm's: a pan leaves `line` alone, a trim moves it up.
  const markers: { line: number; isDisposed: boolean; dispose: () => void }[] = []
  const scrollListeners = new Set<() => void>()
  const resizeListeners = new Set<() => void>()
  const term = {
    element,
    cols: COLS,
    rows: ROWS,
    modes: { mouseTrackingMode: 'none' },
    buffer: {
      active: {
        viewportY: 0,
        type: 'normal' as 'normal' | 'alternate',
        baseY: 0,
        cursorY: 0,
        getLine: (r: number) => {
          const text = lines[r]
          if (text === undefined) return undefined
          return { getCell: (x: number) => (x < COLS ? { getWidth: () => 1, getChars: () => text[x] ?? '' } : undefined) }
        },
      },
    },
    select: vi.fn((col: number, row: number, len: number) => {
      sel = { s: row * COLS + col, len }
      for (const l of [...selListeners]) l()
    }),
    clearSelection: vi.fn(() => { sel = null; for (const l of [...selListeners]) l() }),
    getSelection: () => (sel ? flat.slice(sel.s, sel.s + sel.len) : ''),
    hasSelection: () => sel !== null,
    getSelectionPosition: () => {
      if (!sel) return undefined
      const e = sel.s + sel.len
      return { start: { x: sel.s % COLS, y: Math.floor(sel.s / COLS) }, end: { x: e % COLS, y: Math.floor(e / COLS) } }
    },
    onSelectionChange: on(selListeners),
    registerMarker: (offset: number) => {
      const m = { line: offset, isDisposed: false, dispose: () => { m.isDisposed = true } }
      markers.push(m)
      return m
    },
    onScroll: on(scrollListeners),
    /** A viewport pan: rows stay where they are. */
    pan: () => { for (const l of [...scrollListeners]) l() },
    /** New output at scrollback capacity: every row moves up by one. */
    trim: () => {
      for (const m of markers) m.line -= 1
      for (const l of [...scrollListeners]) l()
    },
    onRender: on(new Set()),
    onResize: on(resizeListeners),
    /** A refit (a web font loads, the phone rotates): xterm reflows to new sizes. */
    resize: (cols: number, rows: number) => {
      term.cols = cols
      term.rows = rows
      // The cell keeps its size, so the grid grows or shrinks with it.
      host.getBoundingClientRect = rect(0, 0, cols * CELL_W, rows * CELL_H)
      screenEl.getBoundingClientRect = rect(0, 0, cols * CELL_W, rows * CELL_H)
      for (const l of [...resizeListeners]) l()
    },
  }
  return { term: term as unknown as Terminal, raw: term, host, element, screenEl, viewport }
}

function touch(target: Element, type: string, x: number, y: number): TouchEvent {
  const t = { clientX: x, clientY: y, pageX: x, pageY: y, target } as unknown as Touch
  const init: TouchEventInit = { bubbles: true, cancelable: true }
  if (type === 'touchstart' || type === 'touchmove') init.touches = [t]
  init.changedTouches = [t]
  const e = new TouchEvent(type, init)
  target.dispatchEvent(e)
  return e
}

/** Centre of cell (col, row) in client px. */
const at = (col: number, row: number) => [col * CELL_W + CELL_W / 2, row * CELL_H + CELL_H / 2] as const

let rafQueue: Map<number, FrameRequestCallback>
let rafId = 0
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance', 'Date'] })
  // A manual frame clock: each flush runs the pending frames 16ms apart.
  rafQueue = new Map()
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafQueue.set(++rafId, cb); return rafId })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => { rafQueue.delete(id) })
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  document.body.replaceChildren()
})
function frames(n: number) {
  for (let i = 0; i < n; i++) {
    vi.advanceTimersByTime(16)
    const due = [...rafQueue.values()]
    rafQueue.clear()
    for (const cb of due) cb(performance.now())
  }
}

function mount(lines: string[], enabled = true) {
  const t = makeTerm(lines)
  const ref = { current: t.host }
  const hook = renderHook(() => useTerminalTouchSelection(t.term, enabled, ref))
  return { ...t, hook }
}

/** A quick upward flick on the grid, as xterm sees it while the finger is down. */
function flick(target: Element, fromY: number, step: number, moves: number) {
  touch(target, 'touchstart', 50, fromY)
  for (let i = 1; i <= moves; i++) {
    vi.advanceTimersByTime(16)
    touch(target, 'touchmove', 50, fromY - step * i)
  }
  return touch(target, 'touchend', 50, fromY - step * moves)
}

describe('touch scrolling', () => {
  it('keeps scrolling after the finger lifts, slowing down until it stops', () => {
    const { screenEl, viewport } = mount(['a', 'b', 'c', 'd'])
    flick(screenEl, 70, 10, 5) // 10px per 16ms ≈ 0.6 px/ms
    const atLift = viewport.scrollTop
    // The first 10px is still a pending press, which xterm scrolls itself (not
    // modelled here); the hook scrolls the 40px after it, 1:1 with the finger.
    expect(atLift).toBe(40)
    frames(5)
    const early = viewport.scrollTop - atLift
    expect(early).toBeGreaterThan(0)
    frames(5)
    const later = viewport.scrollTop - atLift
    // Still moving, but each frame covers less ground than the one before.
    expect(later).toBeGreaterThan(early)
    expect(later - early).toBeLessThan(early)
    frames(400)
    expect(rafQueue.size).toBe(0)
  })

  it.each([
    ['an alternate-screen app', (raw: ReturnType<typeof makeTerm>['raw']) => { raw.buffer.active.type = 'alternate' }],
    ['an app with mouse tracking', (raw: ReturnType<typeof makeTerm>['raw']) => { raw.modes.mouseTrackingMode = 'vt200' }],
  ])('turns a drag into wheel input for %s, one step per row, with no glide', (_name, setup) => {
    const { screenEl, viewport, raw } = mount(['a', 'b', 'c', 'd'])
    setup(raw)
    const wheels: number[] = []
    screenEl.addEventListener('wheel', e => { wheels.push((e as WheelEvent).deltaY) })
    flick(screenEl, 70, 15, 4) // 60px up in 15px moves; rows are 20px
    expect(wheels).toEqual([20, 20, 20])
    expect(viewport.scrollTop).toBe(0)
    frames(10)
    expect(rafQueue.size).toBe(0)
  })

  it('leaves a sideways swipe to the page, so an ancestor gesture still sees it', () => {
    const { screenEl, viewport } = mount(['a', 'b', 'c', 'd'])
    // The side panel's drawer swipe listens in the bubble phase above the grid.
    const seen: boolean[] = []
    const ancestor = (e: Event) => { seen.push(e.defaultPrevented) }
    document.body.addEventListener('touchmove', ancestor)
    try {
      touch(screenEl, 'touchstart', 20, 40)
      touch(screenEl, 'touchmove', 45, 44)
      touch(screenEl, 'touchmove', 70, 46)
      touch(screenEl, 'touchend', 70, 46)
    } finally {
      document.body.removeEventListener('touchmove', ancestor)
    }
    expect(seen).toEqual([false, false])
    expect(viewport.scrollTop).toBe(0)
  })

  it('adds only the travel after the pending press when a scroll starts', () => {
    const { screenEl, viewport } = mount(['a', 'b', 'c', 'd'])
    touch(screenEl, 'touchstart', 50, 70)
    touch(screenEl, 'touchmove', 50, 62) // within tolerance: xterm's move
    touch(screenEl, 'touchmove', 50, 40)
    expect(viewport.scrollTop).toBe(22)
  })

  it('stops a glide after a long frame gap instead of jumping', () => {
    const { screenEl, viewport } = mount(['a', 'b', 'c', 'd'])
    flick(screenEl, 70, 10, 5)
    frames(2)
    const before = viewport.scrollTop
    vi.advanceTimersByTime(10_000) // the tab was in the background
    const due = [...rafQueue.values()]
    rafQueue.clear()
    for (const cb of due) cb(performance.now())
    expect(viewport.scrollTop).toBe(before)
    expect(rafQueue.size).toBe(0)
  })

  it('does not glide when the finger stopped before lifting', () => {
    const { screenEl, viewport } = mount(['a', 'b', 'c', 'd'])
    touch(screenEl, 'touchstart', 50, 70)
    vi.advanceTimersByTime(16)
    touch(screenEl, 'touchmove', 50, 40)
    vi.advanceTimersByTime(300)
    touch(screenEl, 'touchend', 50, 40)
    expect(viewport.scrollTop).toBe(30)
    frames(10)
    expect(viewport.scrollTop).toBe(30)
  })

  it('stops the glide on the next touch', () => {
    const { screenEl, viewport } = mount(['a', 'b', 'c', 'd'])
    flick(screenEl, 70, 10, 5)
    frames(3)
    touch(screenEl, 'touchstart', 50, 40)
    const stoppedAt = viewport.scrollTop
    frames(10)
    expect(viewport.scrollTop).toBe(stoppedAt)
  })

  it('scrolls once per move: xterm does not scroll the viewport a second time', () => {
    const { screenEl, element, viewport } = mount(['a', 'b', 'c', 'd'])
    const xtermMove = vi.fn()
    element.addEventListener('touchmove', xtermMove)
    touch(screenEl, 'touchstart', 50, 70)
    const move = touch(screenEl, 'touchmove', 50, 30)
    expect(viewport.scrollTop).toBe(40)
    expect(move.defaultPrevented).toBe(true)
    expect(xtermMove).not.toHaveBeenCalled()
  })

  it('keeps following a touch whose row was repainted away', () => {
    // xterm's DOM renderer replaces row spans as it paints; the touch keeps
    // targeting the detached span, whose events no longer reach the host.
    const { screenEl, viewport } = mount(['a', 'b', 'c', 'd'])
    const span = document.createElement('span')
    screenEl.append(span)
    touch(span, 'touchstart', 50, 70)
    span.remove()
    touch(span, 'touchmove', 50, 40)
    expect(viewport.scrollTop).toBe(30)
  })
})

describe('long-press selection', () => {
  it('selects the word under the finger, by character', () => {
    const { screenEl, raw, hook } = mount(['', 'say hello', '', ''])
    const [x, y] = at(5, 1) // the "l" of hello
    touch(screenEl, 'touchstart', x, y)
    act(() => { vi.advanceTimersByTime(500) })
    expect(raw.select).toHaveBeenLastCalledWith(4, 1, 5)
    expect(raw.getSelection()).toBe('hello')
    // The lift must not let the emulated mousedown clear the new selection.
    expect(touch(screenEl, 'touchend', x, y).defaultPrevented).toBe(true)
    expect(hook.result.current.handles?.start).toEqual({ left: 40, top: 20, height: 20 })
    expect(hook.result.current.handles?.end).toEqual({ left: 90, top: 20, height: 20 })
  })

  it('turns the knob inward on the first and last visible rows', () => {
    const { screenEl, hook } = mount(['hello', '', '', 'world'])
    const [x, y] = at(1, 0)
    touch(screenEl, 'touchstart', x, y)
    act(() => { vi.advanceTimersByTime(500) })
    expect(hook.result.current.handles?.start).toEqual({ left: 0, top: 0, height: 20, flip: true })
    touch(screenEl, 'touchend', x, y)
    const [bx, by] = at(1, 3)
    touch(screenEl, 'touchstart', bx, by)
    act(() => { vi.advanceTimersByTime(500) })
    expect(hook.result.current.handles?.end).toEqual({ left: 50, top: 60, height: 20, flip: true })
  })

  it('drops a pending press when new output trims scrollback before it fires', () => {
    const { screenEl, raw } = mount(['', 'say hello', '', ''])
    const [x, y] = at(5, 1)
    touch(screenEl, 'touchstart', x, y)
    act(() => { raw.trim() })
    act(() => { vi.advanceTimersByTime(500) })
    expect(raw.select).not.toHaveBeenCalled()
  })

  it('keeps a long-press selection when the pressed row was repainted away', () => {
    const { screenEl, raw } = mount(['', 'say hello', '', ''])
    const span = document.createElement('span')
    screenEl.append(span)
    const [x, y] = at(5, 1)
    touch(span, 'touchstart', x, y)
    act(() => { vi.advanceTimersByTime(500) })
    span.remove()
    // Without preventDefault the browser's emulated mousedown clears it.
    expect(touch(span, 'touchend', x, y).defaultPrevented).toBe(true)
    expect(raw.getSelection()).toBe('hello')
  })

  it('extends the selection while the finger keeps dragging after the press', () => {
    const { screenEl, raw } = mount(['', 'say hello', 'world wide', ''])
    const [x, y] = at(5, 1)
    touch(screenEl, 'touchstart', x, y)
    act(() => { vi.advanceTimersByTime(500) })
    const [x2, y2] = at(4, 2)
    const move = touch(screenEl, 'touchmove', x2, y2)
    expect(move.defaultPrevented).toBe(true)
    expect(raw.getSelection()).toBe('hello world') // the fake joins padded rows with no newline
  })

  it('a moving finger is a scroll, never a selection', () => {
    const { screenEl, raw } = mount(['', 'say hello', '', ''])
    touch(screenEl, 'touchstart', 50, 30)
    touch(screenEl, 'touchmove', 50, 60)
    act(() => { vi.advanceTimersByTime(600) })
    expect(raw.select).not.toHaveBeenCalled()
  })

  it('ignores a press that starts off the grid (an overlaid button)', () => {
    const { host, raw } = mount(['', 'say hello', '', ''])
    const button = document.createElement('button')
    host.append(button)
    touch(button, 'touchstart', 50, 30)
    act(() => { vi.advanceTimersByTime(600) })
    expect(raw.select).not.toHaveBeenCalled()
  })
})

describe('selection handles', () => {
  /** Long-press "hello", lift, and render the end handle's hit area into the host. */
  function selectHello() {
    const m = mount(['', 'say hello', 'world wide', ''])
    const [x, y] = at(5, 1)
    touch(m.screenEl, 'touchstart', x, y)
    act(() => { vi.advanceTimersByTime(500) })
    touch(m.screenEl, 'touchend', x, y)
    const knob = (which: 'start' | 'end') => {
      const el = document.createElement('div')
      el.setAttribute(TOUCH_HANDLE_ATTR, which)
      m.host.append(el)
      return el
    }
    return { ...m, knob }
  }

  it('dragging the end handle moves the end a character at a time', () => {
    const { raw, knob } = selectHello()
    const end = knob('end')
    // Grab the knob right on the last selected cell (col 8, row 1)...
    const [gx, gy] = at(8, 1)
    touch(end, 'touchstart', gx, gy)
    // ...and drag it to col 2 of the next row.
    const [dx, dy] = at(2, 2)
    expect(touch(end, 'touchmove', dx, dy).defaultPrevented).toBe(true)
    expect(raw.getSelection()).toBe('hello wor')
    // Back onto the same row, one cell shorter than the word.
    const [bx, by] = at(7, 1)
    touch(end, 'touchmove', bx, by)
    expect(raw.getSelection()).toBe('hell')
    expect(touch(end, 'touchend', bx, by).defaultPrevented).toBe(true)
  })

  it('drops a handle drag when new output trims scrollback under it, but not on a pan', () => {
    const { raw, knob } = selectHello()
    const end = knob('end')
    const [gx, gy] = at(8, 1)
    touch(end, 'touchstart', gx, gy)
    raw.pan()
    const [dx, dy] = at(2, 2)
    touch(end, 'touchmove', dx, dy)
    expect(raw.getSelection()).toBe('hello wor')
    raw.trim()
    expect(raw.getSelection()).toBe('')
    // The next move no longer drives a selection from the stale rows.
    const [bx, by] = at(7, 1)
    expect(touch(end, 'touchmove', bx, by).defaultPrevented).toBe(false)
    expect(raw.getSelection()).toBe('')
  })

  it('drops a handle drag when a refit changes the column count, but not on a height change', () => {
    const { raw, knob } = selectHello()
    const end = knob('end')
    const [gx, gy] = at(8, 1)
    touch(end, 'touchstart', gx, gy)
    // Only the height changes: every saved cell index still names the same text.
    raw.resize(COLS, ROWS + 1)
    const [dx, dy] = at(2, 2)
    touch(end, 'touchmove', dx, dy)
    expect(raw.getSelection()).toBe('hello wor')
    // The columns change: indices built from the old width now decode elsewhere.
    raw.resize(COLS - 2, ROWS + 1)
    expect(raw.getSelection()).toBe('')
    const [bx, by] = at(7, 1)
    expect(touch(end, 'touchmove', bx, by).defaultPrevented).toBe(false)
    expect(raw.select).toHaveBeenCalledTimes(2)
  })

  it('drops a pending press when a refit changes the column count before it fires', () => {
    const { screenEl, raw } = mount(['', 'say hello', '', ''])
    const [x, y] = at(5, 1)
    touch(screenEl, 'touchstart', x, y)
    act(() => { raw.resize(COLS - 2, ROWS) })
    act(() => { vi.advanceTimersByTime(500) })
    expect(raw.select).not.toHaveBeenCalled()
  })

  it('lets the lift of a selection drag reach an ancestor, so its own gesture can reset', () => {
    const { screenEl, raw, knob } = selectHello()
    // The shell's drawer swipe listens in the bubble phase above the grid and
    // resets only on touchend: a swallowed lift would leave it armed.
    const lifts: string[] = []
    const ancestor = (e: Event) => { lifts.push(e.type) }
    document.body.addEventListener('touchend', ancestor)
    document.body.addEventListener('touchcancel', ancestor)
    try {
      const [x, y] = at(5, 1)
      touch(screenEl, 'touchstart', x, y)
      act(() => { vi.advanceTimersByTime(500) })
      const [dx, dy] = at(7, 2)
      touch(screenEl, 'touchmove', dx, dy)
      expect(touch(screenEl, 'touchend', dx, dy).defaultPrevented).toBe(true)
      const end = knob('end')
      const [gx, gy] = at(8, 2)
      touch(end, 'touchstart', gx, gy)
      touch(end, 'touchmove', gx + 10, gy)
      touch(end, 'touchcancel', gx + 10, gy)
    } finally {
      document.body.removeEventListener('touchend', ancestor)
      document.body.removeEventListener('touchcancel', ancestor)
    }
    expect(lifts).toEqual(['touchend', 'touchcancel'])
    expect(raw.getSelection()).not.toBe('')
  })

  it('dragging the start handle keeps the end fixed', () => {
    const { raw, knob } = selectHello()
    const start = knob('start')
    const [gx, gy] = at(4, 1)
    touch(start, 'touchstart', gx, gy)
    const [dx, dy] = at(0, 1)
    touch(start, 'touchmove', dx, dy)
    expect(raw.getSelection()).toBe('say hello')
  })

  it('the key bar Copy writes exactly the touch selection', async () => {
    const { term, raw } = selectHello()
    const writeText = vi.fn(() => Promise.resolve())
    const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    onTestFinished(() => {
      if (original) Object.defineProperty(navigator, 'clipboard', original)
      else delete (navigator as unknown as Record<string, unknown>).clipboard
    })
    render(<TerminalKeyBar term={term} />)
    expect(raw.getSelection()).toBe('hello')
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await act(async () => { await Promise.resolve() })
    expect(writeText).toHaveBeenCalledWith('hello')
  })
})

describe('mouse devices', () => {
  it('binds nothing when the device is not touch: the mouse path is untouched', () => {
    const { screenEl, viewport, raw, hook } = mount(['', 'say hello', '', ''], false)
    const [x, y] = at(5, 1)
    expect(touch(screenEl, 'touchstart', x, y).defaultPrevented).toBe(false)
    act(() => { vi.advanceTimersByTime(600) })
    flick(screenEl, 70, 10, 5)
    frames(10)
    expect(raw.select).not.toHaveBeenCalled()
    expect(viewport.scrollTop).toBe(0)
    expect(hook.result.current.handles).toBeNull()
  })

  it('leaves mouse events alone on a touch device too', () => {
    const { screenEl, raw } = mount(['', 'say hello', '', ''])
    const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true, clientX: 50, clientY: 30 })
    const up = new MouseEvent('mouseup', { bubbles: true, cancelable: true, clientX: 80, clientY: 30 })
    screenEl.dispatchEvent(down)
    act(() => { vi.advanceTimersByTime(600) })
    screenEl.dispatchEvent(up)
    expect(down.defaultPrevented).toBe(false)
    expect(up.defaultPrevented).toBe(false)
    expect(raw.select).not.toHaveBeenCalled()
    expect(raw.clearSelection).not.toHaveBeenCalled()
  })
})
