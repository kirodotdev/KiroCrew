/**
 * A failed write never throws away what the user typed.
 *
 * The rule under test is the general one: **the UI does not discard user input
 * on an unconfirmed write.** A fire-and-forget `onAdd` must not clear the draft
 * until the write is confirmed — otherwise a typed reminder vanishes (e.g. with
 * the desktop app closed) and the only trace is a muted line at the far end of
 * the page.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import RemindersSection from '../apps/crew-companion/RemindersSection'
import type { RemindersPayload } from '../apps/crew-companion/types'

const payload: RemindersPayload = {
  reminders: [],
  breakNudgesEnabled: true,
  sessionNotificationsEnabled: true,
  breakReminderMins: 45,
  language: 'English',
  present: true,
}

const setup = (onAdd: (t: string, f: string, e?: number) => Promise<boolean>) => {
  render(
    <RemindersSection
      rem={payload}
      remError={null}
      onAdd={onAdd}
      onSkip={vi.fn()}
      onRemove={vi.fn()}
    />,
  )
  // The add box is the only textbox in this section.
  return screen.getByRole('textbox') as HTMLInputElement
}

const type = (input: HTMLInputElement, text: string) => {
  fireEvent.change(input, { target: { value: text } })
  fireEvent.submit(input.closest('form')!)
}

describe('the add box and a failing write', () => {
  it('KEEPS the draft when the write fails', async () => {
    const onAdd = vi.fn().mockResolvedValue(false)
    const input = setup(onAdd)

    type(input, 'drink water in 20 minutes')
    await waitFor(() => expect(onAdd).toHaveBeenCalled())

    // The whole point: the text is still there to retry or copy.
    await waitFor(() => expect(input.value).toBe('drink water in 20 minutes'))
  })

  it('clears the draft when the write succeeds', async () => {
    const onAdd = vi.fn().mockResolvedValue(true)
    const input = setup(onAdd)

    type(input, 'stretch every 2 hours')
    await waitFor(() => expect(onAdd).toHaveBeenCalled())
    await waitFor(() => expect(input.value).toBe(''))
  })

  it('does not call onAdd at all when no time was given', async () => {
    // The parser must not invent a time, so the box asks instead of writing.
    const onAdd = vi.fn().mockResolvedValue(true)
    const input = setup(onAdd)

    type(input, 'buy milk')
    await waitFor(() => expect(input.value).toBe('buy milk'))
    expect(onAdd).not.toHaveBeenCalled()
  })
})
