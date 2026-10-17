import React from 'react'
import { act, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'

import ShortcutsModal from '../components/ShortcutsModal'
import type { ChatSection } from '../hooks/useChatNavigation'
import { SHORTCUTS_ENABLED_KEY, useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import { useTurnStepper } from '../pages/chat/useTurnStepper'
import type { RootState } from '../store'
import { createTestStore, renderHookWithProviders, renderWithProviders } from './helpers'

/**
 * Alt+↑ / Alt+↓ end to end: the global shortcut handler matches the chord and
 * the chat page's stepper (useTurnStepper) picks the turn from a transcript's
 * mounted rows, then hands its display index to the pinned-prompt jump.
 *
 * The scroller is a layout-less DOM with hand-set rects, ten 100px rows, and a
 * reading line 120px below its top. The jump is a mock that moves nothing, so a
 * step that follows another reads the turn the previous one landed on.
 */

const LINE = 120
const ROW_H = 100
// Prompts at display indexes 0, 3, 6 and 8; the rows between are replies.
const TURNS: ChatSection[] = [0, 3, 6, 8].map((displayIdx, i) => ({
  id: `turn-${i}`,
  label: `prompt ${i + 1}`,
  prompt: `prompt ${i + 1}`,
  response: '',
  ts: `2026-10-11T00:00:0${i}.000Z`,
  msgIdx: displayIdx,
  displayIdx,
}))

const mounted: HTMLElement[] = []
afterEach(() => {
  for (const node of mounted.splice(0)) node.remove()
})
beforeEach(() => localStorage.clear())

function setRect(el: HTMLElement, top: number, height: number) {
  Object.defineProperty(el, 'getBoundingClientRect', {
    configurable: true,
    value: () => ({ top, bottom: top + height, left: 0, right: 0, width: 0, height, x: 0, y: top, toJSON: () => ({}) }),
  })
}

/** Mount the transcript scrolled `offset` px down and wire both hooks to it. */
function setup({ offset, active = true, setHighlightTs }: { offset: number; active?: boolean; setHighlightTs?: (update: string | null | ((current: string | null) => string | null)) => void }) {
  const scroller = document.createElement('div')
  setRect(scroller, 0, 600)
  for (let i = 0; i < 10; i++) {
    const row = document.createElement('div')
    row.setAttribute('data-display-index', String(i))
    setRect(row, i * ROW_H - offset, ROW_H)
    scroller.appendChild(row)
  }
  document.body.appendChild(scroller)
  mounted.push(scroller)
  const scrollerRef = { current: scroller }
  const jump = vi.fn()
  const store = createTestStore({
    dashboard: { slots: [{ key: 'slot-1', title: 'Chat 1', messages: 1, running: false }] } as unknown as RootState['dashboard'],
    chat: { activeSlot: 'slot-1', slotHistory: [] } as unknown as RootState['chat'],
  })
  const hook = renderHookWithProviders(() => {
    useKeyboardShortcuts({ onToggleShortcutsModal: () => {}, onNewChat: () => {} })
    return useTurnStepper({ active, turns: TURNS, windowed: false, scrollerRef, landingInset: () => LINE, jump, setHighlightTs })
  }, { store })
  return { hook, jump, scroller }
}

/** Press Alt+↑ / Alt+↓ on `target`; returns whether the keystroke was claimed. */
function press(code: 'ArrowUp' | 'ArrowDown', target: EventTarget = document.body, init: KeyboardEventInit = {}): boolean {
  const event = new KeyboardEvent('keydown', { code, key: code, altKey: true, bubbles: true, cancelable: true, ...init })
  act(() => { target.dispatchEvent(event) })
  return event.defaultPrevented
}

const jumpedTo = (jump: ReturnType<typeof vi.fn>) => jump.mock.calls.map(call => call[0])

describe('Alt+↑ / Alt+↓ step through the turns of the open chat', () => {
  it('from inside a reply, Alt+↑ jumps to that reply\'s prompt and Alt+↓ to the next prompt', () => {
    // Row 5 (a reply to the prompt at 3) sits on the line.
    const up = setup({ offset: 5 * ROW_H - LINE })
    expect(press('ArrowUp')).toBe(true)
    expect(jumpedTo(up.jump)).toEqual([3])
    up.hook.unmount()

    const down = setup({ offset: 5 * ROW_H - LINE })
    expect(press('ArrowDown')).toBe(true)
    expect(jumpedTo(down.jump)).toEqual([6])
  })

  it('steps away from a prompt that sits on the reading line', () => {
    const { jump } = setup({ offset: 6 * ROW_H - LINE })
    press('ArrowDown')
    expect(jumpedTo(jump)).toEqual([8])
  })

  it('chains steps from the turn the last one landed on, even when the transcript cannot scroll there', () => {
    // Clamped at the bottom: the prompts at 6 and 8 both stay below the line
    // whatever the jump does, so the screen alone would offer 6 every time.
    const { jump } = setup({ offset: 300 })
    press('ArrowDown')
    press('ArrowDown')
    expect(jumpedTo(jump)).toEqual([6, 8])
  })

  it('claims the keystroke but goes nowhere past either end', () => {
    const { jump } = setup({ offset: 300 })
    press('ArrowUp')
    press('ArrowUp')
    expect(jumpedTo(jump)).toEqual([3, 0])
    expect(press('ArrowUp')).toBe(true)
    expect(jumpedTo(jump)).toEqual([3, 0])
  })

  it('re-reads the screen after the reader scrolls or presses another key', () => {
    const { jump } = setup({ offset: 300 })
    press('ArrowUp')
    press('ArrowUp')
    expect(jumpedTo(jump)).toEqual([3, 0])
    // From the remembered turn at 0, Alt+↓ would go to 3; from the screen (row 5
    // is the first row below the line) it goes to 6.
    act(() => { document.body.dispatchEvent(new WheelEvent('wheel', { deltaY: 40, bubbles: true })) })
    press('ArrowDown')
    expect(jumpedTo(jump)).toEqual([3, 0, 6])

    press('ArrowUp')
    act(() => { document.body.dispatchEvent(new KeyboardEvent('keydown', { code: 'PageDown', key: 'PageDown', bubbles: true })) })
    press('ArrowUp')
    expect(jumpedTo(jump)).toEqual([3, 0, 6, 3, 3])
  })

  it('announces the turn a step landed on', () => {
    const { hook } = setup({ offset: 300 })
    press('ArrowDown')
    expect(hook.result.current).toBe('Turn 3 of 4: prompt 3')
  })

  it('flashes the landed prompt with the jump highlight, and clears only its own', () => {
    vi.useFakeTimers()
    onTestFinished(() => { vi.useRealTimers() })
    let highlighted: string | null = null
    const setHighlightTs = vi.fn((update: string | null | ((current: string | null) => string | null)) => {
      highlighted = typeof update === 'function' ? update(highlighted) : update
    })
    setup({ offset: 300, setHighlightTs })
    press('ArrowDown')
    expect(highlighted).toBe(TURNS[2].ts)
    act(() => { vi.advanceTimersByTime(2000) })
    expect(highlighted).toBeNull()

    // A highlight another jump set in the meantime survives this one's timer.
    press('ArrowDown')
    expect(highlighted).toBe(TURNS[3].ts)
    highlighted = 'pinned-message-ts'
    act(() => { vi.advanceTimersByTime(2000) })
    expect(highlighted).toBe('pinned-message-ts')
  })

  it('never fires while typing in the composer or any other text field', () => {
    const { jump } = setup({ offset: 300 })
    const textarea = document.createElement('textarea')
    const editable = document.createElement('div')
    editable.contentEditable = 'true'
    document.body.append(textarea, editable)
    mounted.push(textarea, editable)
    expect(press('ArrowUp', textarea)).toBe(false)
    expect(press('ArrowDown', editable)).toBe(false)
    expect(jump).not.toHaveBeenCalled()
  })

  it('leaves a keystroke a focused control already claimed, and Alt+Shift+arrow', () => {
    const { jump } = setup({ offset: 300 })
    const control = document.createElement('button')
    control.addEventListener('keydown', e => e.preventDefault())
    document.body.appendChild(control)
    mounted.push(control)
    press('ArrowDown', control)
    expect(press('ArrowDown', document.body, { shiftKey: true })).toBe(false)
    expect(jump).not.toHaveBeenCalled()
  })

  it('does nothing while shortcuts are turned off', () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const { jump } = setup({ offset: 300 })
    expect(press('ArrowDown')).toBe(false)
    expect(jump).not.toHaveBeenCalled()
  })

  it('leaves the chord to the browser when no transcript is on screen', () => {
    const { jump } = setup({ offset: 300, active: false })
    expect(press('ArrowDown')).toBe(false)
    expect(jump).not.toHaveBeenCalled()
  })
})

describe('the shortcuts reference', () => {
  it('lists the prompt steps under chat navigation with their Alt+arrow chords', () => {
    renderWithProviders(<ShortcutsModal onClose={() => {}} />)
    // jsdom is non-Mac. The label is a block div (an i18n run boundary), so the row is its parent.
    const prev = screen.getByText('Previous prompt in this chat').parentElement!
    const next = screen.getByText('Next prompt in this chat').parentElement!
    expect(prev).toHaveTextContent('Alt')
    expect(prev).toHaveTextContent('\u2191')
    expect(next).toHaveTextContent('Alt')
    expect(next).toHaveTextContent('\u2193')
  })
})
