import { useState } from 'react'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ScheduleLaterPopover from '../components/ScheduleLaterPopover'

/*
 * The picker's keyboard contract, tested against the popover directly so the
 * opener is a plain button the harness controls. `onClose` and `onSchedule`
 * both unmount the popover, the way the composer host does, so every
 * dismissal path exercises the same focus-restore cleanup.
 */
function Host({
  onClose = vi.fn(),
  onSchedule = vi.fn(),
}: {
  onClose?: ReturnType<typeof vi.fn>
  onSchedule?: ReturnType<typeof vi.fn>
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Open picker</button>
      <button type="button">Behind the picker</button>
      {open ? (
        <ScheduleLaterPopover
          anchorRect={new DOMRect(0, 0, 32, 32)}
          onSchedule={at => { onSchedule(at); setOpen(false) }}
          onClose={() => { onClose(); setOpen(false) }}
        />
      ) : null}
    </>
  )
}

/* Opens the picker the way a keyboard user does: the opener holds focus when
 * its click lands, so it is `document.activeElement` on the popover's first
 * render. */
async function openFromKeyboard() {
  const opener = screen.getByRole('button', { name: 'Open picker' })
  opener.focus()
  fireEvent.click(opener)
  const dialog = await screen.findByTestId('schedule-later-popover')
  return { opener, dialog }
}

describe('ScheduleLaterPopover keyboard', () => {
  it('moves focus onto the time field when it opens', async () => {
    renderWithProviders(<Host />)

    await openFromKeyboard()

    expect(screen.getByTestId('schedule-later-at')).toHaveFocus()
  })

  it('closes on Escape and hands focus back to the opener', async () => {
    const onClose = vi.fn()
    renderWithProviders(<Host onClose={onClose} />)
    const { opener } = await openFromKeyboard()

    fireEvent.keyDown(screen.getByTestId('schedule-later-at'), { key: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('schedule-later-popover')).not.toBeInTheDocument())
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(opener).toHaveFocus()
  })

  it('leaves an Escape the IME owns to the composition, not the picker', async () => {
    const onClose = vi.fn()
    renderWithProviders(<Host onClose={onClose} />)
    await openFromKeyboard()

    fireEvent.keyDown(screen.getByTestId('schedule-later-at'), { key: 'Escape', isComposing: true })

    expect(screen.getByTestId('schedule-later-popover')).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('returns focus to the opener after a confirmed schedule unmounts it', async () => {
    const onSchedule = vi.fn()
    renderWithProviders(<Host onSchedule={onSchedule} />)
    const { opener } = await openFromKeyboard()

    fireEvent.click(screen.getByTestId('schedule-later-confirm'))

    await waitFor(() => expect(screen.queryByTestId('schedule-later-popover')).not.toBeInTheDocument())
    expect(onSchedule).toHaveBeenCalledTimes(1)
    expect(opener).toHaveFocus()
  })

  it('does not reclaim focus from the control an outside pointer dismissal landed on', async () => {
    const onClose = vi.fn()
    renderWithProviders(<Host onClose={onClose} />)
    const { opener } = await openFromKeyboard()
    const behind = screen.getByRole('button', { name: 'Behind the picker' })

    // The browser routes focus per the pointer target before React unmounts
    // the picker; the restore must see focus already outside and stand down.
    behind.focus()
    fireEvent.pointerDown(behind)

    await waitFor(() => expect(screen.queryByTestId('schedule-later-popover')).not.toBeInTheDocument())
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(behind).toHaveFocus()
    expect(opener).not.toHaveFocus()
  })

  it('leaves ArrowUp/ArrowDown to the datetime field instead of moving focus', async () => {
    renderWithProviders(<Host />)
    await openFromKeyboard()
    const at = screen.getByTestId('schedule-later-at')

    // `fireEvent` returns false when a listener called preventDefault(): a
    // claimed arrow would stop the field stepping its own segments.
    expect(fireEvent.keyDown(at, { key: 'ArrowDown' })).toBe(true)
    expect(fireEvent.keyDown(at, { key: 'ArrowUp' })).toBe(true)
    expect(at).toHaveFocus()
  })

  it('cycles Tab and Shift+Tab between the time field and the confirm button', async () => {
    renderWithProviders(<Host />)
    await openFromKeyboard()
    const at = screen.getByTestId('schedule-later-at')
    const confirm = screen.getByTestId('schedule-later-confirm')

    expect(confirm).toBeEnabled()
    fireEvent.keyDown(at, { key: 'Tab', shiftKey: true })
    expect(confirm).toHaveFocus()

    fireEvent.keyDown(confirm, { key: 'Tab' })
    expect(at).toHaveFocus()
  })

  it('keeps Escape working once a past time has disabled the confirm button', async () => {
    const onClose = vi.fn()
    renderWithProviders(<Host onClose={onClose} />)
    const { opener } = await openFromKeyboard()
    const at = screen.getByTestId('schedule-later-at')

    fireEvent.change(at, { target: { value: '2020-01-01T00:00' } })
    expect(screen.getByTestId('schedule-later-confirm')).toBeDisabled()
    fireEvent.keyDown(at, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('schedule-later-popover')).not.toBeInTheDocument())
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(opener).toHaveFocus()
  })

  it('discloses the restart limit before the schedule is confirmed', async () => {
    renderWithProviders(<Host />)
    await openFromKeyboard()

    const note = screen.getByTestId('schedule-later-restart-note')
    expect(note).toHaveTextContent(
      "If the gateway restarts before then, this message is cleared and won't be sent.",
    )
    // Read to a screen-reader user as the confirm button's description, so the
    // limit is heard at the point of commitment and not only seen.
    expect(screen.getByTestId('schedule-later-confirm')).toHaveAttribute(
      'aria-describedby',
      note.id,
    )
    // Static disclosure, not an error: a valid time keeps it visible and does
    // not turn it into the past-time status line.
    expect(screen.queryByTestId('schedule-later-at-error')).not.toBeInTheDocument()
    expect(note).not.toHaveAttribute('role')
  })
})
