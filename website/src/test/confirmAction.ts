import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { expect } from 'vitest'

export async function confirmAction(name: string | RegExp): Promise<void> {
  const dialog = await waitFor(() => {
    const found = screen.getAllByRole('dialog').filter(candidate =>
      candidate.querySelector('[data-guide-confirm]'),
    ).at(-1)
    if (!found) throw new Error('Confirmation dialog is not open')
    return found
  })
  const matches = within(dialog).getAllByRole('button', { name })
  // The close control can share the affirmative action's label.
  const action = matches.length === 1
    ? matches[0]
    : matches.find(button => button.hasAttribute('data-guide-confirm'))
  if (!action) throw new Error(`Confirmation dialog has several "${String(name)}" buttons`)
  fireEvent.click(action)
  // The click can remove the dialog before a removal observer subscribes.
  await waitFor(() => expect(dialog).not.toBeInTheDocument())
}
