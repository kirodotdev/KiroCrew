import { act, render } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ScrollPeekLayer from './ScrollPeekLayer'
import {
  PEEK_COLLAPSE_DELAY_MS,
  PEEK_IDLE_MAX_MS,
  PEEK_LEAVE_DELAY_MS,
  PEEK_MIN_OVERFLOW,
  measureClippedTitles,
  useSidebarScrollPeek,
  type PeekTitle,
} from './scrollPeek'

const rect = (top: number, bottom: number, left = 0, right = 200) =>
  ({ top, bottom, left, right, width: right - left, height: bottom - top, x: left, y: top, toJSON: () => ({}) })

/** A title element that clips `overflow` px of its text and sits at `top`. */
function stubTitle(el: HTMLElement, overflow: number, top: number) {
  Object.defineProperty(el, 'clientWidth', { configurable: true, value: 150 })
  Object.defineProperty(el, 'scrollWidth', { configurable: true, value: 150 + overflow })
  el.getBoundingClientRect = () => rect(top, top + 20, 20, 170)
}

function stubLane(lane: HTMLElement, scrolls = true) {
  Object.defineProperty(lane, 'scrollHeight', { configurable: true, value: scrolls ? 1000 : 300 })
  Object.defineProperty(lane, 'clientHeight', { configurable: true, value: 300 })
  lane.getBoundingClientRect = () => rect(0, 300)
}

/** The sidebar's shape: a root of a fixed width holding the scrolling lane. */
function Harness({ enabled = true, onTitles }: { enabled?: boolean; onTitles?: (t: PeekTitle[]) => void }) {
  const rootRef = useRef<HTMLDivElement>(null)
  const laneRef = useRef<HTMLDivElement>(null)
  const titles = useSidebarScrollPeek({ enabled, rootRef, laneRef })
  onTitles?.(titles)
  return (
    <div ref={rootRef} data-testid="root" style={{ width: 200 }} data-count={titles.length}>
      <div data-testid="header">header</div>
      <div ref={laneRef} data-testid="lane">
        <div data-session-row="a"><button type="button" data-testid="row" data-session-title>A very long session title that the row clips</button></div>
        <div data-session-row="b"><div data-testid="short" data-session-title>Short</div></div>
      </div>
    </div>
  )
}

function setup(props: Parameters<typeof Harness>[0] = {}) {
  const r = render(<Harness {...props} />)
  const lane = r.getByTestId('lane')
  stubLane(lane)
  stubTitle(r.getByTestId('row'), 100, 40)
  stubTitle(r.getByTestId('short'), 4, 80)
  return { ...r, lane, root: r.getByTestId('root') }
}

/** A wheel tick, then the frame the hook measures on. */
const wheel = (el: Element, init: WheelEventInit = { deltaY: 40 }) => {
  el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, ...init }))
  vi.advanceTimersByTime(20)
}

describe('measureClippedTitles', () => {
  it('returns only titles clipped by at least the threshold and fully in view', () => {
    const lane = document.createElement('div')
    stubLane(lane)
    const mk = (overflow: number, top: number, key: string) => {
      const row = document.createElement('div')
      row.dataset.sessionRow = key
      const t = document.createElement('div')
      t.setAttribute('data-session-title', '')
      t.textContent = `title ${key}`
      stubTitle(t, overflow, top)
      row.appendChild(t)
      lane.appendChild(row)
    }
    mk(100, 40, 'clipped')
    mk(PEEK_MIN_OVERFLOW - 1, 80, 'barely')
    mk(100, 290, 'half-out')
    mk(100, 2, 'under-dock')
    lane.style.scrollPaddingTop = '10px'
    document.body.appendChild(lane)
    const got = measureClippedTitles(lane, 1000)
    lane.remove()
    expect(got.map(t => t.text)).toEqual(['title clipped'])
    expect(got[0]).toMatchObject({ left: 20, top: 40, height: 20, width: 250, maxWidth: 1000 - 20 - 12 })
  })

  it('skips a title behind a stuck folder header and the row under the pointer', () => {
    const lane = document.createElement('div')
    stubLane(lane)
    const rows: HTMLElement[] = []
    for (const [key, top] of [['behind', 40], ['hovered', 80], ['shown', 120]] as const) {
      const row = document.createElement('div')
      row.dataset.sessionRow = key
      const t = document.createElement('div')
      t.setAttribute('data-session-title', '')
      t.textContent = key
      stubTitle(t, 100, top)
      row.appendChild(t)
      lane.appendChild(row)
      rows.push(row)
    }
    const header = document.createElement('div')
    header.className = 'folder-row-sticky'
    header.getBoundingClientRect = () => rect(30, 62)
    lane.appendChild(header)
    document.body.appendChild(lane)
    const got = measureClippedTitles(lane, 1000, rows[1])
    lane.remove()
    expect(got.map(t => t.text)).toEqual(['shown'])
  })
})

describe('measureClippedTitles occlusion', () => {
  it('skips a title that something outside the lane covers', () => {
    const lane = document.createElement('div')
    stubLane(lane)
    const row = document.createElement('div')
    row.dataset.sessionRow = 'covered'
    const t = document.createElement('div')
    t.setAttribute('data-session-title', '')
    t.textContent = 'covered'
    stubTitle(t, 100, 40)
    row.appendChild(t)
    lane.appendChild(row)
    document.body.appendChild(lane)
    const panel = document.createElement('div')
    document.body.appendChild(panel)
    const original = document.elementFromPoint
    document.elementFromPoint = () => panel
    try {
      expect(measureClippedTitles(lane, 1000)).toEqual([])
      document.elementFromPoint = () => t
      expect(measureClippedTitles(lane, 1000).map(x => x.text)).toEqual(['covered'])
    } finally {
      document.elementFromPoint = original
      lane.remove()
      panel.remove()
    }
  })
})

describe('useSidebarScrollPeek', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('shows the clipped titles after a lane wheel, measured on the next frame', () => {
    const { lane, root } = setup()
    act(() => { lane.dispatchEvent(new WheelEvent('wheel', { deltaY: 40, bubbles: true })) })
    expect(root.dataset.count).toBe('0')
    act(() => { vi.advanceTimersByTime(20) })
    expect(root.dataset.count).toBe('1')
  })

  it('never changes the sidebar width', () => {
    const { lane, root } = setup()
    act(() => { root.dispatchEvent(new Event('pointerenter')); wheel(lane) })
    expect(root.dataset.count).toBe('1')
    expect(root.style.width).toBe('200px')
  })

  it('ignores input outside the lane, horizontal and zoom wheels, and a lane that cannot scroll', () => {
    const { lane, root, getByTestId } = setup()
    act(() => { wheel(getByTestId('header')); wheel(lane, { deltaY: 0 }) })
    // happy-dom's WheelEvent drops `ctrlKey` from its init dict; set it directly.
    const zoom = new WheelEvent('wheel', { deltaY: 40, bubbles: true })
    Object.defineProperty(zoom, 'ctrlKey', { value: true })
    act(() => { lane.dispatchEvent(zoom); vi.advanceTimersByTime(20) })
    expect(root.dataset.count).toBe('0')
    stubLane(lane, false)
    act(() => { wheel(lane) })
    expect(root.dataset.count).toBe('0')
  })

  it('does nothing while disabled, and ends a live peek when turned off', () => {
    const r = setup({ enabled: false })
    act(() => { wheel(r.lane) })
    expect(r.root.dataset.count).toBe('0')
    r.rerender(<Harness />)
    act(() => { r.root.dispatchEvent(new Event('pointerenter')); wheel(r.lane) })
    expect(r.root.dataset.count).toBe('1')
    r.rerender(<Harness enabled={false} />)
    expect(r.root.dataset.count).toBe('0')
  })

  it('follows the rows: a lane scroll re-measures their positions', () => {
    const seen: PeekTitle[][] = []
    const { lane, root, getByTestId } = setup({ onTitles: t => seen.push(t) })
    act(() => { root.dispatchEvent(new Event('pointerenter')); wheel(lane) })
    expect(seen.at(-1)?.[0].top).toBe(40)
    stubTitle(getByTestId('row'), 100, 120)
    act(() => { lane.dispatchEvent(new Event('scroll')); vi.advanceTimersByTime(20) })
    expect(seen.at(-1)?.[0].top).toBe(120)
  })

  it('follows the rows when they move without a scroll (a live re-sort)', async () => {
    const seen: PeekTitle[][] = []
    const { lane, root, getByTestId } = setup({ onTitles: t => seen.push(t) })
    act(() => { root.dispatchEvent(new Event('pointerenter')); wheel(lane) })
    expect(seen.at(-1)?.[0].top).toBe(40)
    stubTitle(getByTestId('row'), 100, 160)
    await act(async () => {
      lane.appendChild(document.createElement('div'))
      await Promise.resolve()
      vi.advanceTimersByTime(20)
    })
    expect(seen.at(-1)?.[0].top).toBe(160)
  })

  it('drops the chip of the row under the pointer, so its hover actions show', () => {
    const { lane, root, container } = setup()
    const [rowA, rowB] = container.querySelectorAll<HTMLElement>('[data-session-row]')
    rowA.getBoundingClientRect = () => rect(40, 60)
    rowB.getBoundingClientRect = () => rect(80, 100)
    const move = (y: number) => {
      const e = new Event('pointermove', { bubbles: true })
      Object.defineProperties(e, { clientX: { value: 100 }, clientY: { value: y } })
      root.dispatchEvent(e)
      vi.advanceTimersByTime(20)
    }
    act(() => { root.dispatchEvent(new Event('pointerenter')); wheel(lane) })
    expect(root.dataset.count).toBe('1')
    act(() => { move(50) })
    expect(root.dataset.count).toBe('0')
    act(() => { move(90) })
    expect(root.dataset.count).toBe('1')
  })

  it('re-resolves the hovered row on each measure, since a wheel scroll fires no pointerover', () => {
    const { lane, root, container, getByTestId } = setup()
    const [rowA, rowB] = container.querySelectorAll<HTMLElement>('[data-session-row]')
    rowA.getBoundingClientRect = () => rect(40, 60)
    rowB.getBoundingClientRect = () => rect(80, 100)
    // The wheel lands with the pointer resting on row A: no chip for A.
    // happy-dom's WheelEvent drops clientX/Y from its init dict; set them directly.
    const tick = new WheelEvent('wheel', { deltaY: 40, bubbles: true })
    Object.defineProperties(tick, { clientX: { value: 100 }, clientY: { value: 50 } })
    act(() => { root.dispatchEvent(new Event('pointerenter')); lane.dispatchEvent(tick); vi.advanceTimersByTime(20) })
    expect(root.dataset.count).toBe('0')
    // The lane scrolls A out from under the still pointer and B in.
    rowA.getBoundingClientRect = () => rect(120, 140)
    rowB.getBoundingClientRect = () => rect(40, 60)
    stubTitle(getByTestId('row'), 100, 120)
    act(() => { lane.dispatchEvent(new Event('scroll')); vi.advanceTimersByTime(20) })
    expect(root.dataset.count).toBe('1')
  })

  it('follows a re-sort slide, which moves rows by style alone', async () => {
    const seen: PeekTitle[][] = []
    const { lane, root, container, getByTestId } = setup({ onTitles: t => seen.push(t) })
    act(() => { root.dispatchEvent(new Event('pointerenter')); wheel(lane) })
    expect(seen.at(-1)?.[0].top).toBe(40)
    stubTitle(getByTestId('row'), 100, 70)
    await act(async () => {
      container.querySelector<HTMLElement>('[data-session-row="a"]')!.style.transform = 'translateY(30px)'
      await Promise.resolve()
      vi.advanceTimersByTime(20)
    })
    expect(seen.at(-1)?.[0].top).toBe(70)
  })

  it('ends at once on a press or a non-scrolling key anywhere on the page', () => {
    const { lane, root } = setup()
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    act(() => { root.dispatchEvent(new Event('pointerenter')); wheel(lane) })
    act(() => { outside.dispatchEvent(new Event('pointerdown', { bubbles: true })) })
    expect(root.dataset.count).toBe('0')
    act(() => { wheel(lane) })
    expect(root.dataset.count).toBe('1')
    act(() => { outside.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', bubbles: true })) })
    expect(root.dataset.count).toBe('0')
    outside.remove()
  })

  it('ends at once when the window loses focus', () => {
    const { lane, root } = setup()
    act(() => { root.dispatchEvent(new Event('pointerenter')); wheel(lane) })
    act(() => { window.dispatchEvent(new Event('blur')) })
    expect(root.dataset.count).toBe('0')
  })

  it('ends after the idle ceiling even with the pointer resting on the sidebar', () => {
    const { lane, root } = setup()
    act(() => { root.dispatchEvent(new Event('pointerenter')); wheel(lane) })
    act(() => { vi.advanceTimersByTime(PEEK_IDLE_MAX_MS - 100) })
    expect(root.dataset.count).toBe('1')
    // A scroll input refreshes the ceiling.
    act(() => { wheel(lane) })
    act(() => { vi.advanceTimersByTime(PEEK_IDLE_MAX_MS - 100) })
    expect(root.dataset.count).toBe('1')
    act(() => { vi.advanceTimersByTime(200) })
    expect(root.dataset.count).toBe('0')
  })

  it('stays on while the pointer is over the sidebar, and ends after it leaves', () => {
    const { lane, root } = setup()
    act(() => { root.dispatchEvent(new Event('pointerenter')); wheel(lane) })
    act(() => { vi.advanceTimersByTime(PEEK_COLLAPSE_DELAY_MS * 3) })
    expect(root.dataset.count).toBe('1')
    act(() => { root.dispatchEvent(new Event('pointerleave')); vi.advanceTimersByTime(PEEK_LEAVE_DELAY_MS) })
    expect(root.dataset.count).toBe('0')
  })

  it('ends at once on any press in the sidebar', () => {
    const { lane, root, getByTestId } = setup()
    act(() => { root.dispatchEvent(new Event('pointerenter')); wheel(lane) })
    act(() => { getByTestId('row').dispatchEvent(new Event('pointerdown', { bubbles: true })) })
    expect(root.dataset.count).toBe('0')
  })

  it('treats touch as no hover and settles after the last touch input', () => {
    const { lane, root } = setup()
    const touch = (type: string) => {
      const e = new Event(type)
      Object.defineProperty(e, 'pointerType', { value: 'touch' })
      return e
    }
    act(() => { root.dispatchEvent(touch('pointerenter')); lane.dispatchEvent(new Event('touchmove', { bubbles: true })); vi.advanceTimersByTime(20) })
    expect(root.dataset.count).toBe('1')
    act(() => { root.dispatchEvent(touch('pointerleave')); vi.advanceTimersByTime(PEEK_LEAVE_DELAY_MS + 50) })
    expect(root.dataset.count).toBe('1')
    act(() => { vi.advanceTimersByTime(PEEK_COLLAPSE_DELAY_MS) })
    expect(root.dataset.count).toBe('0')
  })

  it('reacts to scrolling keys but not to caret keys in a text field', () => {
    const { lane, root, getByTestId } = setup()
    const input = document.createElement('textarea')
    lane.appendChild(input)
    act(() => { input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true })); vi.advanceTimersByTime(20) })
    expect(root.dataset.count).toBe('0')
    act(() => { getByTestId('row').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true })); vi.advanceTimersByTime(20) })
    expect(root.dataset.count).toBe('1')
    act(() => { vi.advanceTimersByTime(PEEK_COLLAPSE_DELAY_MS) })
    expect(root.dataset.count).toBe('0')
  })
})

describe('ScrollPeekLayer', () => {
  it('renders nothing when there is nothing to peek', () => {
    render(<ScrollPeekLayer titles={[]} />)
    expect(document.querySelector('[data-testid="scroll-peek-layer"]')).toBeNull()
  })

  it('draws each title over its row in a click-through layer, capped at the window edge', () => {
    const r = render(<ScrollPeekLayer titles={[{ key: 'a', text: 'Full title', left: 20, top: 40, height: 20, width: 600, maxWidth: 300 }]} />)
    const layer = document.querySelector<HTMLElement>('[data-testid="scroll-peek-layer"]')
    expect(layer?.className).toContain('pointer-events-none')
    expect(layer?.getAttribute('aria-hidden')).toBe('true')
    const chip = document.querySelector<HTMLElement>('[data-scroll-peek-title]')
    expect(chip?.textContent).toBe('Full title')
    expect(chip?.style.width).toBe('300px')
    r.unmount()
  })
})
