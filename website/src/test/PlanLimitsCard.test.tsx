/**
 * The Usage view's plan-limits card: one Codex row, and a detail card that a
 * hover, keyboard focus, or a click or tap opens. Every state says what Codex
 * reported and nothing more: no reading, no plan limits, a stale window, a
 * window near its limit.
 */
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { HOVER_CLOSE_MS, HOVER_MIN_VISIBLE_MS, HOVER_OPEN_MS } from '../hooks/useHoverIntent'
import { PlanLimitsCard, planLimitRowState } from '../pages/overview/PlanLimitsCard'
import type { PlanLimitReading, PlanLimits } from '../api/planLimits'

const NOW = Date.parse('2026-11-10T15:00:00Z')
const HOUR = 3_600_000

function codex(fiveHourUsed: number, weeklyUsed: number, opts: { fiveHourResetsAt?: number } = {}): PlanLimitReading {
  return {
    fiveHour: { usedPercent: fiveHourUsed, resetsAt: opts.fiveHourResetsAt ?? NOW + 2 * HOUR, windowMinutes: 300, stale: false },
    weekly: { usedPercent: weeklyUsed, resetsAt: NOW + 4 * 24 * HOUR, windowMinutes: 10080, stale: false },
    capturedAt: NOW - 3 * 60_000,
  }
}

/** Pin the row's box, so a test can move it the way a page scroll would. */
function placeRow(row: HTMLElement, top: number) {
  row.getBoundingClientRect = () => ({ top, bottom: top + 40, left: 20, right: 700, width: 680, height: 40, x: 20, y: top, toJSON: () => ({}) })
}

const NO_READING: PlanLimits = { codex: null }

/** Set the viewport height for one test and put the old value back when it ends. */
function setViewportHeight(height: number) {
  const original = Object.getOwnPropertyDescriptor(window, 'innerHeight')
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
  onTestFinished(() => {
    if (original) Object.defineProperty(window, 'innerHeight', original)
    else Reflect.deleteProperty(window, 'innerHeight')
  })
}

function mount(props: { limits?: PlanLimits; defaultBackend?: string }) {
  return render(<PlanLimitsCard limits={props.limits} defaultBackend={props.defaultBackend} />)
}

function mouseIn(el: HTMLElement) {
  fireEvent.pointerEnter(el, { pointerType: 'mouse' })
  fireEvent.mouseEnter(el)
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('when the card shows', () => {
  it('an install that runs only Kiro and has no reading gets no card', () => {
    const { container } = mount({ limits: NO_READING, defaultBackend: 'kas' })
    expect(container).toBeEmptyDOMElement()
  })

  it('a Codex reading gets the row whatever the default harness is', () => {
    mount({ limits: { codex: codex(1, 1) }, defaultBackend: 'kas' })
    expect(screen.getByTestId('plan-limits-row-codex')).toBeInTheDocument()
  })

  it('a Codex install gets the row before Codex has reported anything', () => {
    mount({ limits: NO_READING, defaultBackend: 'codex' })
    expect(screen.getByTestId('plan-limits-row-codex')).toBeInTheDocument()
  })
})

describe('the row', () => {
  it('shows each window the provider named, by its own length, with the used share', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    expect(row).toHaveTextContent('Codex')
    expect(row).toHaveTextContent('5-hour 24%')
    expect(row).toHaveTextContent('7-day 63%')
  })

  it('colors a window near or at its limit in the danger tone', () => {
    mount({ limits: { codex: codex(95, 100) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    expect(within(row).getByText('5-hour 95%')).toHaveClass('text-danger')
    expect(within(row).getByText('7-day 100%')).toHaveClass('text-danger')
  })

  it('shows a dash, not the old share, for a window that reset before the report arrived', () => {
    mount({ limits: { codex: codex(80, 10, { fiveHourResetsAt: NOW - 60_000 }) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    expect(row).toHaveTextContent('5-hour \u2014')
    expect(row).not.toHaveTextContent('80%')
  })

  it('says there is no reading yet, not 0%', () => {
    mount({ limits: NO_READING, defaultBackend: 'codex' })
    const row = screen.getByTestId('plan-limits-row-codex')
    expect(row).toHaveTextContent('No reading yet')
    expect(row).not.toHaveTextContent('%')
  })

  it('says a sign-in with no plan windows has no plan limits', () => {
    mount({ limits: { codex: { fiveHour: null, weekly: null, capturedAt: NOW } } })
    expect(screen.getByTestId('plan-limits-row-codex')).toHaveTextContent('No plan limits')
  })
})

describe('the detail card', () => {
  it('a hover shows used, left and the reset time as a tooltip', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    mouseIn(row)
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS) })
    const card = screen.getByRole('tooltip')
    expect(row).toHaveAttribute('aria-describedby', card.id)
    const fiveHour = within(card).getByTestId('plan-limits-window-five_hour')
    expect(fiveHour).toHaveTextContent('5-hour window')
    expect(fiveHour).toHaveTextContent('24% used · 76% left')
    expect(fiveHour).toHaveTextContent('Resets Nov 10, 2026, 5:00 PM')
    expect(card).toHaveTextContent('Reported 3m ago')
    expect(card).toHaveTextContent('From the session logs that Codex writes on this machine.')
  })

  it('a click pins it as a dialog with focus inside, and Escape hands focus back without reopening it', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    fireEvent.click(row)
    const dialog = screen.getByRole('dialog', { name: 'OpenAI Codex plan limits' })
    expect(row).toHaveAttribute('aria-expanded', 'true')
    expect(document.activeElement).toBe(dialog)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
    expect(document.activeElement).toBe(row)
    expect(row).toHaveAttribute('aria-expanded', 'false')
  })

  it('keyboard focus shows the card without moving focus, and Escape dismisses it', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    act(() => { row.focus() })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    expect(document.activeElement).toBe(row)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a focus card that a click focused closes when focus leaves the card', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    act(() => { row.focus() })
    const card = screen.getByRole('tooltip')
    act(() => { card.focus() })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    act(() => { card.blur() })
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
  })

  it('Escape in a focus card that a click focused hands focus back to the row', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    act(() => { row.focus() })
    act(() => { screen.getByRole('tooltip').focus() })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
    expect(document.activeElement).toBe(row)
  })

  it('Escape in a hover card that a click focused hands focus back to the row without reopening it', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    mouseIn(row)
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS) })
    act(() => { screen.getByRole('tooltip').focus() })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
    expect(document.activeElement).toBe(row)
  })

  it('Escape in a hover card leaves focus where the user was typing', () => {
    const input = document.createElement('input')
    document.body.appendChild(input)
    onTestFinished(() => { input.remove() })
    mount({ limits: { codex: codex(24, 63) } })
    act(() => { input.focus() })
    mouseIn(screen.getByTestId('plan-limits-row-codex'))
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS) })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
    expect(document.activeElement).toBe(input)
  })

  it('Escape after focus left a pinned card closes it and leaves focus where the user went', () => {
    const input = document.createElement('input')
    document.body.appendChild(input)
    onTestFinished(() => { input.remove() })
    mount({ limits: { codex: codex(24, 63) } })
    fireEvent.click(screen.getByTestId('plan-limits-row-codex'))
    expect(document.activeElement).toBe(screen.getByRole('dialog'))
    act(() => { input.focus() })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
    expect(document.activeElement).toBe(input)
  })

  it('a resize closes a pinned card that holds focus and hands focus back to the row', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    fireEvent.click(row)
    expect(document.activeElement).toBe(screen.getByRole('dialog'))
    act(() => { window.dispatchEvent(new Event('resize')) })
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
    expect(document.activeElement).toBe(row)
  })

  it('a hover card that a click focused hands focus back to the row when the hover closes', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    mouseIn(row)
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS) })
    act(() => { screen.getByRole('tooltip').focus() })
    fireEvent.mouseLeave(row)
    act(() => { vi.advanceTimersByTime(HOVER_CLOSE_MS + HOVER_MIN_VISIBLE_MS) })
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
    expect(document.activeElement).toBe(row)
  })

  it('one Escape dismisses the focus card even after a hover or ArrowDown on the focused row', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    act(() => { row.focus() })
    fireEvent.keyDown(row, { key: 'ArrowDown' })
    mouseIn(row)
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS) })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('plan-limits-detail')).toBeNull()
  })

  it('a tap pins the card, so a touch screen gets the same detail', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    fireEvent.pointerEnter(row, { pointerType: 'touch' })
    fireEvent.pointerDown(row, { pointerType: 'touch' })
    fireEvent.pointerUp(row, { pointerType: 'touch' })
    fireEvent.mouseEnter(row)
    fireEvent.click(row)
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS * 2) })
    expect(screen.getByRole('dialog', { name: 'OpenAI Codex plan limits' })).toBeInTheDocument()
  })

  it('a scroll that moves the row closes the card, which is placed from a snapshot of the row', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    placeRow(row, 100)
    fireEvent.click(row)
    fireEvent.scroll(window)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    placeRow(row, 40)
    const focusRow = vi.spyOn(row, 'focus')
    fireEvent.scroll(window)
    expect(screen.queryByRole('dialog')).toBeNull()
    // The pinned card held focus, so focus goes back to the row, not to the
    // page body, and without a scroll that would undo the user's own scroll.
    expect(document.activeElement).toBe(row)
    expect(focusRow).toHaveBeenCalledWith({ preventScroll: true })
  })

  it('a scroll inside the card keeps it open, so a tall card can be read to the end', () => {
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    placeRow(row, 100)
    fireEvent.click(row)
    const dialog = screen.getByRole('dialog')
    placeRow(row, 40)
    fireEvent.scroll(dialog)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('opens above a row near the bottom of a short viewport, inside the viewport', () => {
    setViewportHeight(320)
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    placeRow(row, 260)
    fireEvent.click(row)
    const card = screen.getByRole('dialog')
    // The row spans 260 to 300 in a 320px viewport. The card ends 6px above the row, at 254, and its height reaches the 8px top margin.
    expect(card.style.top).toBe('')
    expect(card.style.bottom).toBe('66px')
    expect(card.style.maxHeight).toBe('246px')
    // A scroll that leaves the row in place keeps the card above it, and one that moves the row closes it.
    fireEvent.scroll(window)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    placeRow(row, 200)
    fireEvent.scroll(window)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('opens below the row when there is room, with its height capped to the space below', () => {
    setViewportHeight(768)
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    placeRow(row, 100)
    fireEvent.click(row)
    const card = screen.getByRole('dialog')
    expect(card.style.top).toBe('146px')
    expect(card.style.bottom).toBe('')
    expect(card.style.maxHeight).toBe('614px')
  })

  it('stays below the row with less than 120px free there when even less is free above, capped to the space below', () => {
    setViewportHeight(200)
    mount({ limits: { codex: codex(24, 63) } })
    const row = screen.getByTestId('plan-limits-row-codex')
    placeRow(row, 60)
    fireEvent.click(row)
    const card = screen.getByRole('dialog')
    // The row spans 60 to 100 in a 200px viewport: 86px free below, 46px above.
    expect(card.style.top).toBe('106px')
    expect(card.style.bottom).toBe('')
    expect(card.style.maxHeight).toBe('86px')
  })

  it('explains why a provider has no reading', () => {
    mount({ limits: NO_READING, defaultBackend: 'codex' })
    fireEvent.click(screen.getByTestId('plan-limits-row-codex'))
    expect(screen.getByRole('dialog')).toHaveTextContent('No log on this machine has them yet.')
  })

  it('names a stale window and its reset instead of the old share', () => {
    mount({ limits: { codex: codex(80, 10, { fiveHourResetsAt: NOW - 60_000 }) } })
    fireEvent.click(screen.getByTestId('plan-limits-row-codex'))
    const fiveHour = screen.getByTestId('plan-limits-window-five_hour')
    expect(fiveHour).toHaveTextContent('after this reading')
    expect(fiveHour).not.toHaveTextContent('80%')
  })
})

describe('planLimitRowState', () => {
  it('judges a reset against the clock it is given, not only the server flag', () => {
    const state = planLimitRowState(codex(50, 50, { fiveHourResetsAt: NOW + 1000 }), NOW + 2000)
    expect(state.kind).toBe('windows')
    if (state.kind === 'windows') expect(state.windows.map(w => [w.key, w.stale])).toEqual([['five_hour', true], ['weekly', false]])
  })
})
