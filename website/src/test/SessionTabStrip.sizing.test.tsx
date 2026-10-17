/**
 * SessionTabStrip sizing and repeated close, Chrome's model.
 *
 * Every tab takes one width whatever its title. After a POINTER close the
 * widths freeze and a spacer holds the strip's content width, so the next
 * tab's close button lands under the resting pointer and a user can close a
 * run of tabs without moving the mouse. Leaving the strip reflows it.
 *
 * happy-dom does no layout, so the tests pin the two things layout is computed
 * from: the inline sizing each tab carries, and the rects the strip measured
 * at the moment of the close (stubbed per tab below).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { useState } from 'react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import SessionTabStrip from '../components/SessionTabStrip'
import { freezeAfterPointerClose, TAB_GAP, TAB_MAX_WIDTH, TAB_MIN_WIDTH } from '../lib/sessionTabs'

type Slots = RootState['dashboard']['slots']

const SLOTS = [
  { key: 'a', title: 'Short' },
  { key: 'b', title: 'A considerably longer session title that will truncate' },
  { key: 'c', title: 'x' },
  { key: 'd', title: 'Another very long title describing a debugging session' },
  { key: 'e', title: 'Notes' },
] as unknown as Slots

/** Laid-out width each tab reports when the strip measures it. */
const W = 120

function stubRects() {
  // Tab i sits at left = 10 + i * (W + gap), in the order currently rendered.
  return vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const tab = this.closest('[role="tab"]')
    if (!tab || tab !== this) return new DOMRect(0, 0, 0, 0)
    const all = Array.from(document.querySelectorAll('[role="tab"]'))
    const left = 10 + all.indexOf(tab) * (W + TAB_GAP)
    return new DOMRect(left, 0, W, 28)
  })
}

function Harness({ initial, onClose }: { initial: string[]; onClose?: (k: string) => void }) {
  const [tabs, setTabs] = useState(initial)
  return (
    <>
      <button type="button" onClick={() => setTabs(t => [...t, 'f'])}>open another</button>
      <SessionTabStrip
        tabs={tabs}
        activeKey={tabs[0] ?? null}
        onSelect={vi.fn()}
        onClose={k => { onClose?.(k); setTabs(t => t.filter(x => x !== k)) }}
      />
    </>
  )
}

function renderHarness(initial = ['a', 'b', 'c', 'd', 'e'], onClose?: (k: string) => void) {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: { ...defaults.dashboard, slots: [...SLOTS, { key: 'f', title: 'New' }] as unknown as Slots, unreadSlots: [] },
  })
  return render(<Provider store={store}><Harness initial={initial} onClose={onClose} /></Provider>)
}

const tab = (k: string) => screen.getByTestId(`session-tab-${k}`)
const closeBtn = (k: string) => tab(k).querySelector('button') as HTMLButtonElement
/** A real mouse click: detail 1. A keyboard-synthesized click has detail 0. */
const mouseClose = (k: string) => fireEvent.click(closeBtn(k), { detail: 1 })

afterEach(() => vi.restoreAllMocks())

describe('SessionTabStrip equal widths', () => {
  it('gives every tab the same flex sizing whatever its title length', () => {
    renderHarness()
    const styles = ['a', 'b', 'c', 'd', 'e'].map(k => {
      const s = tab(k).style
      return `${s.flex}|${s.minWidth}|${s.maxWidth}|${s.width}`
    })
    expect(new Set(styles).size).toBe(1)
    expect(tab('a').style.flex).toBe(`1 1 ${TAB_MAX_WIDTH}px`)
    expect(tab('a').style.minWidth).toBe(`${TAB_MIN_WIDTH}px`)
    expect(tab('a').style.maxWidth).toBe(`${TAB_MAX_WIDTH}px`)
  })

  it('lets the title truncate instead of sizing the tab', () => {
    renderHarness()
    const title = tab('b').querySelector('span:not([aria-hidden])') as HTMLElement
    expect(title.className).toContain('truncate')
    expect(title.className).toContain('min-w-0')
    // The close button keeps its size at the tab's right edge.
    expect(closeBtn('b').className).toContain('shrink-0')
  })
})

describe('SessionTabStrip repeated pointer close', () => {
  it('freezes widths so the next close button lands where the last one was', () => {
    stubRects()
    renderHarness()
    mouseClose('b')
    // Every tab is held at the width the closed tab had, and the spacer holds
    // its slot, so tab c now starts where b started: same close-button x.
    for (const k of ['a', 'c', 'd', 'e']) {
      expect(tab(k).style.width).toBe(`${W}px`)
      expect(tab(k).style.flex).toBe('0 0 auto')
    }
    expect(screen.getByTestId('session-tab-spacer').style.width).toBe(`${W}px`)
    // Tab c is now second, and with every tab at W the second close button is
    // where b's was: the slot did not change size.
    expect(screen.getAllByRole('tab')[1]).toBe(tab('c'))

    // A second click on the same spot closes c and keeps the sequence going.
    mouseClose('c')
    expect(tab('d').style.width).toBe(`${W}px`)
    expect(screen.getByTestId('session-tab-spacer').style.width).toBe(`${2 * (W + TAB_GAP) - TAB_GAP}px`)
  })

  it('widens the remaining tabs when the LAST tab closes so the new last close lands under the pointer', () => {
    stubRects()
    renderHarness()
    mouseClose('e')
    // Five tabs of W ended at 10 + 5W + 4 gaps; four tabs fill that span.
    const span = 5 * W + 4 * TAB_GAP
    const expected = (span - 3 * TAB_GAP) / 4
    expect(Number.parseFloat(tab('d').style.width)).toBeCloseTo(expected)
    expect(screen.queryByTestId('session-tab-spacer')).toBeNull()
  })

  it('freezes on a middle-click close too', () => {
    stubRects()
    renderHarness()
    fireEvent(tab('b'), new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 }))
    expect(tab('c').style.width).toBe(`${W}px`)
  })

  it('does not freeze on a touch tap, whose pointerleave fires before the click', () => {
    stubRects()
    renderHarness()
    const btn = closeBtn('b')
    fireEvent.pointerDown(btn, { pointerType: 'touch' })
    fireEvent.pointerLeave(screen.getByTestId('session-tab-strip'), { pointerType: 'touch' })
    fireEvent.click(btn, { detail: 1 })
    expect(screen.queryByTestId('session-tab-b')).toBeNull()
    expect(tab('c').style.flex).toBe(`1 1 ${TAB_MAX_WIDTH}px`)
    expect(screen.queryByTestId('session-tab-spacer')).toBeNull()
    // A mouse close after the tap freezes again.
    fireEvent.pointerDown(closeBtn('c'), { pointerType: 'mouse' })
    fireEvent.click(closeBtn('c'), { detail: 1 })
    expect(tab('d').style.flex).toBe('0 0 auto')
  })

  it('reflows the strip when the pointer leaves it', () => {
    stubRects()
    renderHarness()
    mouseClose('b')
    expect(tab('c').style.flex).toBe('0 0 auto')
    fireEvent.pointerLeave(screen.getByTestId('session-tab-strip'))
    expect(tab('c').style.flex).toBe(`1 1 ${TAB_MAX_WIDTH}px`)
    expect(tab('c').style.width).toBe('')
    expect(screen.queryByTestId('session-tab-spacer')).toBeNull()
  })

  it('reflows on a window resize', () => {
    stubRects()
    renderHarness()
    mouseClose('b')
    act(() => { window.dispatchEvent(new Event('resize')) })
    expect(tab('c').style.flex).toBe(`1 1 ${TAB_MAX_WIDTH}px`)
  })

  it('reflows when a new tab opens mid-sequence', () => {
    stubRects()
    renderHarness()
    mouseClose('b')
    fireEvent.click(screen.getByText('open another'))
    expect(tab('f').style.flex).toBe(`1 1 ${TAB_MAX_WIDTH}px`)
    expect(tab('c').style.flex).toBe(`1 1 ${TAB_MAX_WIDTH}px`)
  })

  it('closes exactly the clicked tab and nothing else', () => {
    stubRects()
    const closed: string[] = []
    renderHarness(undefined, k => closed.push(k))
    mouseClose('b')
    mouseClose('c')
    expect(closed).toEqual(['b', 'c'])
  })
})

describe('SessionTabStrip keyboard close does not freeze', () => {
  it('Delete on a tab reflows at once and keeps focus on the neighbour', () => {
    stubRects()
    renderHarness()
    tab('b').focus()
    fireEvent.keyDown(tab('b'), { key: 'Delete' })
    expect(tab('c').style.flex).toBe(`1 1 ${TAB_MAX_WIDTH}px`)
    expect(document.activeElement).toBe(tab('c'))
  })

  it('Delete ends a pointer sequence already in progress', () => {
    stubRects()
    renderHarness()
    mouseClose('b')
    fireEvent.keyDown(tab('c'), { key: 'Delete' })
    expect(tab('d').style.flex).toBe(`1 1 ${TAB_MAX_WIDTH}px`)
  })

  it('Enter on the close button (a detail-0 click) does not freeze', () => {
    stubRects()
    renderHarness()
    fireEvent.click(closeBtn('b'), { detail: 0 })
    expect(screen.queryByTestId('session-tab-b')).toBeNull()
    expect(tab('c').style.flex).toBe(`1 1 ${TAB_MAX_WIDTH}px`)
  })

  it('keeps tab roles and selection intact while frozen', () => {
    stubRects()
    renderHarness()
    mouseClose('b')
    expect(screen.getAllByRole('tab')).toHaveLength(4)
    expect(tab('a').getAttribute('aria-selected')).toBe('true')
    // The spacer is not a tab and is hidden from assistive tech.
    expect(screen.getByTestId('session-tab-spacer').getAttribute('aria-hidden')).toBe('true')
  })
})

describe('freezeAfterPointerClose', () => {
  const geo = { firstLeft: 0, closedRight: 0, count: 6 }

  it('keeps the width and adds the closed slot for a middle tab', () => {
    expect(freezeAfterPointerClose(null, { width: 100, isLast: false }, geo)).toEqual({ width: 100, spacer: 104 })
  })

  it('accumulates the spacer across a crowded sequence at the minimum width', () => {
    let f = freezeAfterPointerClose(null, { width: TAB_MIN_WIDTH, isLast: false }, geo)
    f = freezeAfterPointerClose(f, { width: TAB_MIN_WIDTH, isLast: false }, geo)
    expect(f).toEqual({ width: TAB_MIN_WIDTH, spacer: 2 * (TAB_MIN_WIDTH + TAB_GAP) })
  })

  it('widens to fill the span for the last tab, capped at the maximum', () => {
    // 6 tabs of 100 + 5 gaps end at 620; 5 tabs + 4 gaps fill it at 120.8.
    expect(freezeAfterPointerClose(null, { width: 100, isLast: true }, { firstLeft: 0, closedRight: 620, count: 6 }).width).toBeCloseTo(120.8)
    // Two tabs of 190 ending at 384: one tab would need 384px.
    expect(freezeAfterPointerClose(null, { width: 190, isLast: true }, { firstLeft: 0, closedRight: 384, count: 2 }).width).toBe(TAB_MAX_WIDTH)
  })

  it('never narrows the frozen width on a last-tab close', () => {
    const prev = { width: 150, spacer: 0 }
    expect(freezeAfterPointerClose(prev, { width: 150, isLast: true }, { firstLeft: 0, closedRight: 100, count: 3 }).width).toBe(150)
  })
})
