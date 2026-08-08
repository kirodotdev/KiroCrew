import { fireEvent, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import Modal from '../components/Modal'
import { renderWithProviders } from './helpers'

/**
 * A realistic host: a trigger button OUTSIDE the dialog plus content inside it.
 * Focus restore can only be asserted against a real trigger, and the trap can
 * only be asserted when there is something outside the dialog to escape to.
 */
function Harness({
  title = 'Danger zone',
  ariaLabel,
  children,
}: {
  title?: React.ReactNode
  ariaLabel?: string
  children?: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button onClick={() => setOpen(true)}>Open dialog</button>
      <button>Behind the overlay</button>
      <Modal open={open} onClose={() => setOpen(false)} title={title} ariaLabel={ariaLabel}>
        {children ?? (
          <>
            <button>First action</button>
            <button>Last action</button>
          </>
        )}
      </Modal>
    </>
  )
}

/** Open the harness dialog the way a user does — focusing the trigger first. */
async function openDialog(): Promise<HTMLElement> {
  const trigger = screen.getByRole('button', { name: 'Open dialog' })
  trigger.focus()
  fireEvent.click(trigger)
  await screen.findByRole('dialog')
  return trigger
}

describe('Modal — dialog semantics', () => {
  it('names the dialog from its own title, with no per-call-site opt-in', async () => {
    renderWithProviders(<Harness />)
    await openDialog()

    const dialog = screen.getByRole('dialog', { name: 'Danger zone' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    // Named by reference to the rendered title, so a title containing an icon
    // (or any element) still yields a usable name.
    expect(dialog).toHaveAttribute('aria-labelledby')
    expect(dialog).not.toHaveAttribute('aria-label')
  })

  it('derives a name from a title that is not a plain string', async () => {
    renderWithProviders(
      <Harness title={<span><span aria-hidden="true">★</span> Composed title</span>} />,
    )
    await openDialog()

    expect(screen.getByRole('dialog', { name: 'Composed title' })).toBeInTheDocument()
  })

  it('lets an explicit ariaLabel override the title-derived name', async () => {
    renderWithProviders(<Harness title="Truncated header" ariaLabel="Explicit dialog name" />)
    await openDialog()

    const dialog = screen.getByRole('dialog', { name: 'Explicit dialog name' })
    // The override wins outright: no labelledby left behind to disagree with it.
    expect(dialog).not.toHaveAttribute('aria-labelledby')
  })
})

describe('Modal — focus management', () => {
  it('moves focus into the dialog on open', async () => {
    renderWithProviders(<Harness />)
    const trigger = await openDialog()

    const dialog = screen.getByRole('dialog')
    expect(dialog).toContainElement(document.activeElement as HTMLElement)
    expect(trigger).not.toHaveFocus()
  })

  it('traps Tab and Shift+Tab within the dialog', async () => {
    renderWithProviders(<Harness />)
    await openDialog()

    const dialog = screen.getByRole('dialog')
    const close = screen.getByRole('button', { name: 'Close' })
    const last = screen.getByRole('button', { name: 'Last action' })

    // Forward from the last control wraps to the first, instead of landing on
    // the page behind the overlay.
    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(close).toHaveFocus()

    // Backward from the first control wraps to the last.
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()

    // And focus that somehow sits outside is pulled back in.
    const outside = screen.getByRole('button', { name: 'Behind the overlay' })
    outside.focus()
    fireEvent.keyDown(outside, { key: 'Tab' })
    expect(dialog).toContainElement(document.activeElement as HTMLElement)
  })

  it('restores focus to the trigger when the dialog closes', async () => {
    renderWithProviders(<Harness />)
    const trigger = await openDialog()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('dismisses on Escape and still restores focus', async () => {
    renderWithProviders(<Harness />)
    const trigger = await openDialog()

    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('handles a dialog whose body has no focusable children', async () => {
    renderWithProviders(<Harness><p>Nothing to interact with.</p></Harness>)
    await openDialog()

    const dialog = screen.getByRole('dialog')
    const close = screen.getByRole('button', { name: 'Close' })
    expect(close).toHaveFocus()

    // A single focusable child cycles to itself rather than throwing or
    // releasing focus to the page.
    expect(() => fireEvent.keyDown(close, { key: 'Tab' })).not.toThrow()
    expect(dialog).toContainElement(document.activeElement as HTMLElement)
  })

  it('releases the page scroll lock on close', async () => {
    renderWithProviders(<Harness />)
    await openDialog()
    expect(document.body.style.overflow).toBe('hidden')

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(document.body.style.overflow).not.toBe('hidden'))
  })

  it('does not touch focus while closed', () => {
    const onClose = vi.fn()
    renderWithProviders(
      <Modal open={false} onClose={onClose} title="Closed">
        <button>Hidden action</button>
      </Modal>,
    )

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(document.body).toHaveFocus()
    // No stray window listener from a closed modal.
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })
})
