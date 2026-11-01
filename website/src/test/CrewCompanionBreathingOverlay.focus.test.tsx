/**
 * Breathing overlay — an aria-modal dialog that takes keyboard focus when it
 * opens, keeps Tab inside, and gives focus back on close.
 *
 * Focus left on the panel behind the overlay, or a Tab that walks out of it,
 * reaches controls the overlay hides. Escape stays on the overlay's own window
 * listener and still ends the exercise once.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import BreathingOverlay from '../apps/crew-companion/BreathingOverlay'

beforeEach(() => {
  // The exercise's frame loop is not under test; keep it from scheduling.
  vi.stubGlobal('requestAnimationFrame', () => 1)
  vi.stubGlobal('cancelAnimationFrame', () => {})
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  document.body.replaceChildren()
})

function openOverlay() {
  const trigger = document.createElement('button')
  trigger.textContent = 'Breathe'
  document.body.appendChild(trigger)
  trigger.focus()
  const onEnd = vi.fn()
  const view = render(<BreathingOverlay onDone={vi.fn()} onEnd={onEnd} />)
  return { trigger, onEnd, view, dialog: screen.getByRole('dialog') }
}

describe('BreathingOverlay focus', () => {
  it('moves focus into the dialog when it opens', () => {
    const { dialog } = openOverlay()
    expect(dialog.contains(document.activeElement)).toBe(true)
  })

  it('pulls a Tab from behind the overlay back inside, and wraps Tab and Shift+Tab', () => {
    const { trigger, dialog } = openOverlay()
    const end = screen.getByRole('button', { name: /end/i })
    // jsdom has no layout, so give the button an offsetParent for the trap's visibility filter.
    Object.defineProperty(end, 'offsetParent', { configurable: true, get: () => dialog })
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Tab' })
    expect(document.activeElement).toBe(end)
    fireEvent.keyDown(end, { key: 'Tab' })
    expect(document.activeElement).toBe(end)
    fireEvent.keyDown(end, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(end)
  })

  it('gives focus back to the trigger when it closes', () => {
    const { trigger, view } = openOverlay()
    expect(document.activeElement).not.toBe(trigger)
    view.unmount()
    expect(document.activeElement).toBe(trigger)
  })

  it('still ends the exercise once per Escape', () => {
    const { onEnd } = openOverlay()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onEnd).toHaveBeenCalledTimes(1)
  })
})
