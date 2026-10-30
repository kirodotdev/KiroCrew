/**
 * SessionTabStrip reordering: the drag wiring and its keyboard twin.
 *
 * A drag is framer's `Reorder.Group` driven by `useLongPressReorder`, and
 * jsdom has no layout, so framer never measures a neighbour to swap with.
 * These cases pin what the strip itself decides: which presses arm a drag
 * (the `data-dragging` cue and how the held tab is drawn), that a swipe stays
 * a scroll, that a click still selects, that a new order does not scroll the
 * strip, and that the keyboard move keeps plain arrows switching sessions.
 */
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { LONG_PRESS_MS, LONG_PRESS_SLOP_PX } from '../hooks/useLongPressReorder'
import { render, screen, fireEvent, renderHook, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import SessionTabStrip from '../components/SessionTabStrip'
import { useSessionTabs } from '../hooks/useSessionTabs'
import { useState } from 'react'

type Slots = RootState['dashboard']['slots']

const SLOTS = [
  { key: 'chat-1', title: 'Design review' },
  { key: 'chat-2', title: 'Debugging the reaper' },
  { key: 'chat-3', title: 'Release notes' },
] as unknown as Slots

function renderStrip(opts: { activeKey?: string } = {}) {
  const onSelect = vi.fn()
  const onClose = vi.fn()
  const onReorder = vi.fn()
  render(
    <Provider store={slotsStore()}>
      <SessionTabStrip
        tabs={['chat-1', 'chat-2', 'chat-3']}
        activeKey={opts.activeKey ?? 'chat-1'}
        onSelect={onSelect}
        onClose={onClose}
        onReorder={onReorder}
      />
    </Provider>,
  )
  const strip = screen.getByTestId('session-tab-strip')
  const tabs = ['chat-1', 'chat-2', 'chat-3'].map(k => screen.getByTestId(`session-tab-${k}`))
  return { onSelect, onClose, onReorder, strip, tabs }
}

function slotsStore() {
  const defaults = createTestStore().getState()
  return createTestStore({ dashboard: { ...defaults.dashboard, slots: SLOTS, unreadSlots: [] } })
}

function touchmovePrevented(): boolean {
  const ev = new Event('touchmove', { cancelable: true, bubbles: true })
  document.dispatchEvent(ev)
  return ev.defaultPrevented
}

describe('SessionTabStrip mouse drag', () => {
  it('holds a tab from a primary press until the pointer is released', () => {
    const { tabs } = renderStrip()
    fireEvent.pointerDown(tabs[1], { clientX: 150, clientY: 10, pointerId: 1, button: 0, pointerType: 'mouse' })
    expect(tabs[1]).toHaveAttribute('data-dragging', 'true')
    fireEvent.pointerUp(window, { clientX: 150, clientY: 10, pointerId: 1, pointerType: 'mouse' })
    expect(tabs[1]).not.toHaveAttribute('data-dragging')
  })

  it('draws the held tab above its neighbours with the held ring', () => {
    const { tabs } = renderStrip()
    expect(tabs[1]).not.toHaveClass('!z-1')
    expect(tabs[1]).not.toHaveClass('ring-accent')
    fireEvent.pointerDown(tabs[1], { clientX: 150, clientY: 10, pointerId: 1, button: 0, pointerType: 'mouse' })
    expect(tabs[1]).toHaveAttribute('data-dragging', 'true')
    // Reorder.Item writes its own inline z-index, so the lift is an
    // important utility that outranks it rather than an inline style.
    expect(tabs[1]).toHaveClass('relative', '!z-1', 'ring-1', 'ring-accent')
    fireEvent.pointerUp(window, { clientX: 150, clientY: 10, pointerId: 1, pointerType: 'mouse' })
    expect(tabs[1]).not.toHaveClass('!z-1')
    expect(tabs[1]).not.toHaveClass('ring-accent')
  })

  it('still selects on a click', () => {
    const { onSelect, onReorder, tabs } = renderStrip()
    fireEvent.pointerDown(tabs[1], { clientX: 150, clientY: 10, pointerId: 1, button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(window, { clientX: 150, clientY: 10, pointerId: 1, pointerType: 'mouse' })
    fireEvent.click(tabs[1])
    expect(onSelect).toHaveBeenCalledWith('chat-2')
    expect(onReorder).not.toHaveBeenCalled()
  })

  it('does not start a drag from the close button or a non-primary mouse button', () => {
    const { onClose, tabs } = renderStrip()
    const close = tabs[0].querySelector('button') as HTMLElement
    fireEvent.pointerDown(close, { clientX: 90, pointerId: 1, button: 0, pointerType: 'mouse' })
    expect(tabs[0]).not.toHaveAttribute('data-dragging')
    fireEvent.click(close, { detail: 1 })
    expect(onClose).toHaveBeenCalledWith('chat-1')
    fireEvent.pointerDown(tabs[1], { clientX: 150, pointerId: 2, button: 1, pointerType: 'mouse' })
    expect(tabs[1]).not.toHaveAttribute('data-dragging')
  })

  it('claims both drawer swipe directions', () => {
    const { strip } = renderStrip()
    expect(strip.parentElement?.parentElement).toHaveAttribute('data-owns-swipe', 'left right')
  })

  it('keeps the pointer-close width freeze on reorderable tabs', () => {
    const rects = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this.getAttribute('role') !== 'tab') return new DOMRect(0, 0, 0, 0)
      const all = Array.from(document.querySelectorAll('[role="tab"]'))
      return new DOMRect(10 + all.indexOf(this) * 124, 0, 120, 28)
    })
    try {
      function Harness() {
        const [tabs, setTabs] = useState(['chat-1', 'chat-2', 'chat-3'])
        return (
          <SessionTabStrip tabs={tabs} activeKey={tabs[0]} onSelect={() => {}}
            onClose={k => setTabs(t => t.filter(x => x !== k))}
            onReorder={setTabs} />
        )
      }
      render(<Provider store={slotsStore()}><Harness /></Provider>)
      const close = screen.getByTestId('session-tab-chat-1').querySelector('button') as HTMLElement
      fireEvent.pointerDown(close, { pointerType: 'mouse', button: 0 })
      fireEvent.click(close, { detail: 1 })
      expect(screen.getByTestId('session-tab-strip')).toHaveAttribute('data-frozen', 'true')
      expect(screen.getByTestId('session-tab-chat-2').style.width).toBe('120px')
    } finally {
      rects.mockRestore()
    }
  })

  it('reveals the active tab when a tab opens, not when the order changes', () => {
    const scrolled = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrolled
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    const store = slotsStore()
    const strip = (tabs: string[]) => (
      <Provider store={store}>
        <SessionTabStrip tabs={tabs} activeKey="chat-1" onSelect={() => {}} onClose={() => {}} onReorder={() => {}} />
      </Provider>
    )
    const { rerender } = render(strip(['chat-1', 'chat-2']))
    scrolled.mockClear()
    // A drag commits each step as a new order of the same tabs.
    rerender(strip(['chat-2', 'chat-1']))
    expect(scrolled).not.toHaveBeenCalled()
    rerender(strip(['chat-2', 'chat-1', 'chat-3']))
    expect(scrolled).toHaveBeenCalledTimes(1)
  })

  it('keeps a keyboard-moved tab in view, though a drag reorder does not scroll', () => {
    const scrolled = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrolled
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    function Harness() {
      const [tabs, setTabs] = useState(['chat-1', 'chat-2', 'chat-3'])
      return <SessionTabStrip tabs={tabs} activeKey="chat-1" onSelect={() => {}} onClose={() => {}} onReorder={setTabs} />
    }
    render(<Provider store={slotsStore()}><Harness /></Provider>)
    const tab = screen.getByTestId('session-tab-chat-2')
    act(() => tab.focus())
    scrolled.mockClear()
    // Moving left keeps the focused node in place, so focus() never runs and
    // only the explicit reveal brings the tab into view.
    fireEvent.keyDown(tab, { key: 'ArrowLeft', ctrlKey: true, shiftKey: true })
    expect(screen.getAllByRole('tab')[0]).toBe(screen.getByTestId('session-tab-chat-2'))
    expect(scrolled.mock.contexts).toContain(screen.getByTestId('session-tab-chat-2'))
  })
})

describe('SessionTabStrip touch reorder', () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }) })
  afterEach(() => { vi.useRealTimers() })

  it('leaves a swipe to the browser so the strip still scrolls', () => {
    const { tabs } = renderStrip()
    fireEvent.pointerDown(tabs[0], { clientX: 50, clientY: 10, pointerId: 1, pointerType: 'touch' })
    fireEvent.pointerMove(window, { clientX: 50 + LONG_PRESS_SLOP_PX + 1, clientY: 10, pointerId: 1, pointerType: 'touch' })
    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS + 50) })
    expect(tabs[0]).not.toHaveAttribute('data-dragging')
    expect(touchmovePrevented()).toBe(false)
  })

  it('arms after a stationary hold and blocks the pan until release', () => {
    const { tabs } = renderStrip()
    fireEvent.pointerDown(tabs[0], { clientX: 50, clientY: 10, pointerId: 1, pointerType: 'touch' })
    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS - 1) })
    expect(tabs[0]).not.toHaveAttribute('data-dragging')
    act(() => { vi.advanceTimersByTime(1) })
    expect(tabs[0]).toHaveAttribute('data-dragging', 'true')
    expect(touchmovePrevented()).toBe(true)
    // Android's long-press menu would cancel the armed touch.
    const menu = new MouseEvent('contextmenu', { cancelable: true, bubbles: true })
    tabs[0].dispatchEvent(menu)
    expect(menu.defaultPrevented).toBe(true)
    fireEvent.pointerUp(window, { clientX: 50, clientY: 10, pointerId: 1, pointerType: 'touch' })
    expect(tabs[0]).not.toHaveAttribute('data-dragging')
    expect(touchmovePrevented()).toBe(false)
  })

  it('keeps a quick tap a plain switch', () => {
    const { onReorder, onSelect, tabs } = renderStrip()
    fireEvent.pointerDown(tabs[1], { clientX: 150, clientY: 10, pointerId: 1, pointerType: 'touch' })
    fireEvent.pointerUp(window, { clientX: 150, clientY: 10, pointerId: 1, pointerType: 'touch' })
    fireEvent.click(tabs[1])
    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS + 50) })
    expect(tabs[1]).not.toHaveAttribute('data-dragging')
    expect(onSelect).toHaveBeenCalledWith('chat-2')
    expect(onReorder).not.toHaveBeenCalled()
  })
})

describe('SessionTabStrip keyboard reorder', () => {
  it('moves the focused tab one slot with Ctrl+Shift+Arrow or Cmd+Shift+Arrow', () => {
    const { onReorder, onSelect, tabs } = renderStrip({ activeKey: 'chat-2' })
    fireEvent.keyDown(tabs[1], { key: 'ArrowRight', ctrlKey: true, shiftKey: true })
    expect(onReorder).toHaveBeenLastCalledWith(['chat-1', 'chat-3', 'chat-2'])
    fireEvent.keyDown(tabs[1], { key: 'ArrowLeft', metaKey: true, shiftKey: true })
    expect(onReorder).toHaveBeenLastCalledWith(['chat-2', 'chat-1', 'chat-3'])
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('does nothing past either end of the strip', () => {
    const { onReorder, tabs } = renderStrip()
    fireEvent.keyDown(tabs[0], { key: 'ArrowLeft', ctrlKey: true, shiftKey: true })
    fireEvent.keyDown(tabs[2], { key: 'ArrowRight', ctrlKey: true, shiftKey: true })
    expect(onReorder).not.toHaveBeenCalled()
  })

  it('keeps focus on the moved tab so the move can repeat', () => {
    function Harness() {
      const [tabs, setTabs] = useState(['chat-1', 'chat-2', 'chat-3'])
      return (
        <SessionTabStrip
          tabs={tabs}
          activeKey="chat-1"
          onSelect={() => {}}
          onClose={() => {}}
          onReorder={next => {
            // A browser blurs a focused node that React re-inserts; jsdom
            // does not, so the harness stands in for that step.
            ;(document.activeElement as HTMLElement | null)?.blur()
            setTabs(next)
          }}
        />
      )
    }
    render(<Provider store={slotsStore()}><Harness /></Provider>)
    const tab = screen.getByTestId('session-tab-chat-1')
    act(() => tab.focus())
    fireEvent.keyDown(tab, { key: 'ArrowRight', ctrlKey: true, shiftKey: true })
    expect(document.activeElement).toBe(screen.getByTestId('session-tab-chat-1'))
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'ArrowRight', ctrlKey: true, shiftKey: true })
    expect(screen.getAllByRole('tab').map(t => t.getAttribute('data-testid'))).toEqual(
      ['session-tab-chat-2', 'session-tab-chat-3', 'session-tab-chat-1'],
    )
    expect(document.activeElement).toBe(screen.getByTestId('session-tab-chat-1'))
  })

  it('leaves a plain arrow switching sessions', () => {
    const { onReorder, onSelect, tabs } = renderStrip()
    fireEvent.keyDown(tabs[0], { key: 'ArrowRight' })
    expect(onSelect).toHaveBeenCalledWith('chat-2')
    expect(onReorder).not.toHaveBeenCalled()
  })
})

describe('useSessionTabs reorderTabs', () => {
  const LIVE = [{ key: 'chat-1' }, { key: 'chat-2' }, { key: 'chat-3' }]

  it('reorders and persists the set without firing the swap cue', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2', 'chat-3']))
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-1', LIVE, true))
    act(() => result.current.reorderTabs(['chat-2', 'chat-1', 'chat-3']))
    expect(result.current.tabs).toEqual(['chat-2', 'chat-1', 'chat-3'])
    expect(localStorage.getItem('mc-session-tabs-chat')).toBe('["chat-2","chat-1","chat-3"]')
    expect(result.current.cue).toBeNull()
    localStorage.clear()
  })

  it('ignores an order that is not a permutation of the open tabs', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2', 'chat-3']))
    onTestFinished(() => localStorage.clear())
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-1', LIVE, true))
    const before = result.current.tabs
    const rejected = [
      ['chat-2', 'chat-1'],
      ['chat-2', 'chat-1', 'chat-3', 'chat-4'],
      ['chat-2', 'chat-1', 'chat-4'],
      ['chat-2', 'chat-2', 'chat-1'],
    ]
    for (const keys of rejected) {
      act(() => result.current.reorderTabs(keys))
      expect(result.current.tabs).toBe(before)
    }
    // The same order keeps the same array, so nothing re-renders or re-saves.
    act(() => result.current.reorderTabs(['chat-1', 'chat-2', 'chat-3']))
    expect(result.current.tabs).toBe(before)
  })

  it('is a no-op on a surface that does not own the set', () => {
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-1', LIVE, false))
    act(() => result.current.reorderTabs(['chat-2', 'chat-1', 'chat-3']))
    expect(result.current.tabs).toEqual([])
    expect(localStorage.getItem('mc-session-tabs-chat')).toBeNull()
  })
})
