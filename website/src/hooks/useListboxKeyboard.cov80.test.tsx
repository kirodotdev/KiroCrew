/**
 * useListboxKeyboard — roving-focus keyboard navigation for a portal listbox.
 *
 * Real DOM focus is the observable here, so the harness renders an actual
 * listbox with `data-option` buttons and asserts `document.activeElement` after
 * each key. Covers both auto-focus-on-open branches (aria-selected option vs
 * first option, and the touch/filter-input skips), every arrow/Home/End edge,
 * and the two keys that must be consumed (Escape/Tab) versus the ones that must
 * fall through while the caret is in the filter input.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { useRef } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

const touch = vi.hoisted(() => ({ value: false }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => touch.value }))

import { useListboxKeyboard } from './useListboxKeyboard'

interface HarnessProps {
  open?: boolean
  hasFilterInput?: boolean
  filteredCount?: number
  selectedIndex?: number
  onEnterSingleMatch?: () => void
  closeToTrigger?: () => void
}

function Harness({
  open = true,
  hasFilterInput = false,
  filteredCount = 3,
  selectedIndex = -1,
  onEnterSingleMatch = () => {},
  closeToTrigger = () => {},
}: HarnessProps) {
  const dropdownRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const { onListKeyDown } = useListboxKeyboard({
    open,
    dropdownRef,
    inputRef,
    hasFilterInput,
    filteredCount,
    onEnterSingleMatch,
    closeToTrigger,
  })
  return (
    <div
      ref={dropdownRef}
      role="listbox"
      tabIndex={-1}
      aria-label="zzq listbox"
      data-testid="zzq-list"
      onKeyDown={onListKeyDown}
    >
      {hasFilterInput && <input ref={inputRef} aria-label="zzq filter" data-testid="zzq-input" />}
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          role="option"
          data-option=""
          tabIndex={-1}
          aria-selected={i === selectedIndex}
          data-testid={`zzq-opt-${i}`}
        >
          {`zzq option ${i}`}
        </div>
      ))}
    </div>
  )
}

const list = () => screen.getByTestId('zzq-list')
const opt = (i: number) => screen.getByTestId(`zzq-opt-${i}`)

beforeEach(() => {
  touch.value = false
})

describe('focus on open', () => {
  it('focuses the first option when nothing is selected', async () => {
    render(<Harness />)
    await waitFor(() => expect(document.activeElement).toBe(opt(0)))
  })

  it('focuses the aria-selected option instead of the first', async () => {
    render(<Harness selectedIndex={2} />)
    await waitFor(() => expect(document.activeElement).toBe(opt(2)))
  })

  it('does not steal focus when a filter input is rendered', async () => {
    render(<Harness hasFilterInput />)
    await new Promise((r) => setTimeout(r, 5))
    expect(document.activeElement).not.toBe(opt(0))
  })

  it('does not steal focus on a touch device', async () => {
    touch.value = true
    render(<Harness />)
    await new Promise((r) => setTimeout(r, 5))
    expect(document.activeElement).not.toBe(opt(0))
  })

  it('does not steal focus while closed', async () => {
    render(<Harness open={false} />)
    await new Promise((r) => setTimeout(r, 5))
    expect(document.activeElement).not.toBe(opt(0))
  })
})

describe('closing keys', () => {
  it.each(['Escape', 'Tab'])('%s closes to the trigger and is consumed', (key) => {
    const closeToTrigger = vi.fn()
    render(<Harness closeToTrigger={closeToTrigger} />)
    const notPrevented = fireEvent.keyDown(list(), { key })
    expect(closeToTrigger).toHaveBeenCalledTimes(1)
    expect(notPrevented).toBe(false)
  })
})

describe('arrow navigation', () => {
  it('ArrowDown from the filter input lands on the first option', async () => {
    render(<Harness hasFilterInput />)
    screen.getByTestId('zzq-input').focus()
    fireEvent.keyDown(list(), { key: 'ArrowDown' })
    await waitFor(() => expect(document.activeElement).toBe(opt(0)))
  })

  it('ArrowDown advances one option', async () => {
    render(<Harness />)
    opt(0).focus()
    fireEvent.keyDown(list(), { key: 'ArrowDown' })
    await waitFor(() => expect(document.activeElement).toBe(opt(1)))
  })

  it('ArrowDown on the last option stays there (no wrap)', async () => {
    render(<Harness />)
    opt(2).focus()
    fireEvent.keyDown(list(), { key: 'ArrowDown' })
    await waitFor(() => expect(document.activeElement).toBe(opt(2)))
  })

  it('ArrowUp steps back one option', async () => {
    render(<Harness />)
    opt(2).focus()
    fireEvent.keyDown(list(), { key: 'ArrowUp' })
    await waitFor(() => expect(document.activeElement).toBe(opt(1)))
  })

  it('ArrowUp from the first option returns to the filter input', async () => {
    render(<Harness hasFilterInput />)
    opt(0).focus()
    fireEvent.keyDown(list(), { key: 'ArrowUp' })
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('zzq-input')))
  })

  it('ArrowUp from the first option stays put when there is no input', async () => {
    render(<Harness />)
    opt(0).focus()
    fireEvent.keyDown(list(), { key: 'ArrowUp' })
    await waitFor(() => expect(document.activeElement).toBe(opt(0)))
  })
})

describe('Home / End', () => {
  it('Home jumps to the first option', async () => {
    render(<Harness />)
    opt(2).focus()
    fireEvent.keyDown(list(), { key: 'Home' })
    await waitFor(() => expect(document.activeElement).toBe(opt(0)))
  })

  it('End jumps to the last option', async () => {
    render(<Harness />)
    opt(0).focus()
    fireEvent.keyDown(list(), { key: 'End' })
    await waitFor(() => expect(document.activeElement).toBe(opt(2)))
  })

  it.each(['Home', 'End'])('%s is left to the caret while in the filter input', (key) => {
    render(<Harness hasFilterInput />)
    const input = screen.getByTestId('zzq-input')
    input.focus()
    const notPrevented = fireEvent.keyDown(list(), { key })
    expect(notPrevented).toBe(true)
    expect(document.activeElement).toBe(input)
  })
})

describe('Enter', () => {
  it('selects the sole match from the filter input', () => {
    const onEnterSingleMatch = vi.fn()
    render(<Harness hasFilterInput filteredCount={1} onEnterSingleMatch={onEnterSingleMatch} />)
    screen.getByTestId('zzq-input').focus()
    const notPrevented = fireEvent.keyDown(list(), { key: 'Enter' })
    expect(onEnterSingleMatch).toHaveBeenCalledTimes(1)
    expect(notPrevented).toBe(false)
  })

  it('does nothing from the input when several options still match', () => {
    const onEnterSingleMatch = vi.fn()
    render(<Harness hasFilterInput filteredCount={2} onEnterSingleMatch={onEnterSingleMatch} />)
    screen.getByTestId('zzq-input').focus()
    fireEvent.keyDown(list(), { key: 'Enter' })
    expect(onEnterSingleMatch).not.toHaveBeenCalled()
  })

  it('leaves Enter on a focused option to the native button click', () => {
    const onEnterSingleMatch = vi.fn()
    render(<Harness hasFilterInput filteredCount={1} onEnterSingleMatch={onEnterSingleMatch} />)
    opt(1).focus()
    fireEvent.keyDown(list(), { key: 'Enter' })
    expect(onEnterSingleMatch).not.toHaveBeenCalled()
  })

  it('ignores an unrelated key', () => {
    const closeToTrigger = vi.fn()
    render(<Harness closeToTrigger={closeToTrigger} />)
    const notPrevented = fireEvent.keyDown(list(), { key: 'a' })
    expect(notPrevented).toBe(true)
    expect(closeToTrigger).not.toHaveBeenCalled()
  })
})
