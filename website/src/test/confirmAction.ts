import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { expect } from 'vitest'

/** Answers the open confirmation dialog with the button named `name`. */
export async function confirmAction(name: string | RegExp): Promise<void> {
  const dialog = await waitFor(() => {
    // The innermost match: a confirmation can open inside another dialog.
    const found = screen.getAllByRole('dialog').filter(candidate =>
      candidate.querySelector('[data-guide-confirm]'),
    ).at(-1)
    if (!found) throw new Error('Confirmation dialog is not open')
    return found
  })
  const matches = within(dialog).getAllByRole('button', { name })
  // A dialog's own close control can share the confirm button's name.
  const action = matches.length === 1
    ? matches[0]
    : matches.find(button => button.hasAttribute('data-guide-confirm'))
  if (!action) throw new Error(`Confirmation dialog has several "${String(name)}" buttons`)
  fireEvent.click(action)
  // Not waitForElementToBeRemoved: an answer can unmount the dialog during the
  // click, and that helper refuses an element that is already gone.
  await waitFor(() => expect(dialog).not.toBeInTheDocument())
}
