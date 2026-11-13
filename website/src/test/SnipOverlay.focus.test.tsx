/**
 * Screen-snip crop overlay — an aria-modal dialog that takes keyboard focus
 * when it opens, keeps Tab inside, and gives focus back on close.
 *
 * Focus left on the composer behind the overlay, or a Tab that walks out of
 * it, reaches controls the overlay hides. Escape stays on the overlay's own
 * bubble-phase window listener.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import SnipOverlay from '../components/SnipOverlay'

const frame = {
  width: 400,
  height: 200,
  toDataURL: () => 'data:image/png;base64,AAAA',
} as unknown as HTMLCanvasElement

function openOverlay() {
  const trigger = document.createElement('button')
  trigger.textContent = 'Snip'
  document.body.appendChild(trigger)
  trigger.focus()
  const onCancel = vi.fn()
  const view = render(<SnipOverlay frame={frame} onComplete={vi.fn()} onCancel={onCancel} />)
  return { trigger, onCancel, view, dialog: screen.getByRole('dialog') }
}

afterEach(() => {
  cleanup()
  document.body.replaceChildren()
})

describe('SnipOverlay focus', () => {
  it('moves focus into the dialog when it opens', () => {
    const { dialog } = openOverlay()
    expect(dialog.contains(document.activeElement)).toBe(true)
  })

  it('pulls a Tab from behind the overlay back inside, and wraps Tab and Shift+Tab', () => {
    const { trigger, dialog } = openOverlay()
    const cancel = screen.getByRole('button', { name: /cancel/i })
    // jsdom has no layout, so give the button an offsetParent for the trap's visibility filter.
    Object.defineProperty(cancel, 'offsetParent', { configurable: true, get: () => dialog })
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Tab' })
    expect(document.activeElement).toBe(cancel)
    fireEvent.keyDown(cancel, { key: 'Tab' })
    expect(document.activeElement).toBe(cancel)
    fireEvent.keyDown(cancel, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(cancel)
  })

  it('gives focus back to the trigger when it closes', () => {
    const { trigger, view } = openOverlay()
    expect(document.activeElement).not.toBe(trigger)
    view.unmount()
    expect(document.activeElement).toBe(trigger)
  })

  it('still cancels once per Escape', () => {
    const { onCancel } = openOverlay()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
  })
})
