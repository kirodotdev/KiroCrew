/**
 * AppStoreDialog — the App store's overlay dialogs keep keyboard focus inside
 * while open and hand it back on close.
 *
 * The two uninstall confirms (Library, app detail) and the screenshot viewer
 * render through this component; the page-level specs pin each surface, this
 * one pins the contract itself: focus in on open, Tab / Shift+Tab wrap at the
 * edges, Escape and a backdrop click close, and focus returns to the control
 * that opened the dialog.
 */
import { useState } from 'react'
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import AppStoreDialog from '../components/appstore/AppStoreDialog'

function Harness() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Open dialog</button>
      <button type="button">Behind the dialog</button>
      {open && (
        <AppStoreDialog onClose={() => setOpen(false)} aria-label="Test dialog">
          <div role="presentation" onClick={e => e.stopPropagation()}>
            <button type="button">First</button>
            <button type="button">Last</button>
          </div>
        </AppStoreDialog>
      )}
    </>
  )
}

function openDialog() {
  render(<Harness />)
  const trigger = screen.getByRole('button', { name: 'Open dialog' })
  trigger.focus()
  fireEvent.click(trigger)
  return { trigger, dialog: screen.getByRole('dialog', { name: 'Test dialog' }) }
}

describe('AppStoreDialog', () => {
  it('moves focus to the first control and wraps Tab at both edges', () => {
    const { dialog } = openDialog()
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    const first = screen.getByRole('button', { name: 'First' })
    const last = screen.getByRole('button', { name: 'Last' })
    expect(document.activeElement).toBe(first)

    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(document.activeElement).toBe(first)

    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it('closes on Escape and returns focus to the trigger', () => {
    const { trigger } = openDialog()
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('closes on a backdrop click but not on a click inside the card', () => {
    const { trigger, dialog } = openDialog()
    fireEvent.click(screen.getByRole('button', { name: 'First' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.click(dialog)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })
})
