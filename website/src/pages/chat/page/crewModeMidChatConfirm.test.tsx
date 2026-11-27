/**
 * The confirm before conductor mode turns on mid-chat, through the REAL dialog
 * host: what it shows, and that "Don't show this notice again" is saved only on
 * Confirm and then skips the dialog.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import AppConfirmHost from '../../../components/AppConfirmHost'
import { CREW_MODE_MIDCHAT_CONFIRM_DISMISSED_KEY, confirmCrewModeMidChat } from './crewModeMidChatConfirm'

const TITLE = 'Turn on conductor mode for this chat?'

/** Starts the ask and hands back its pending answer. */
function ask(): Promise<boolean> {
  let outcome: Promise<boolean> = Promise.resolve(false)
  act(() => { outcome = confirmCrewModeMidChat() })
  return outcome
}

describe('confirm before conductor mode turns on mid-chat', () => {
  beforeEach(() => { localStorage.clear(); render(<AppConfirmHost />) })
  afterEach(() => localStorage.clear())

  it('explains conductor mode, states in the warning color that it cannot be undone, and confirms with a primary button', async () => {
    const outcome = ask()
    const dialog = await screen.findByRole('dialog', { name: TITLE })
    expect(within(dialog).getByTestId('crew-mode-intro')).toBeInTheDocument()
    expect(within(dialog).getByRole('list', { name: 'How conductor mode works' })).toBeInTheDocument()
    const line = within(dialog).getByTestId('crew-mode-midchat-lock-line')
    expect(line).toHaveTextContent("Conductor mode can't be turned off for this chat.")
    expect(line.className).toContain('text-warn-text')
    const confirm = within(dialog).getByRole('button', { name: 'Turn on conductor mode' })
    expect(confirm.className).toContain('bg-accent')
    expect(confirm.className).not.toContain('text-danger')
    // The checkbox is named by its own label and comes before the buttons.
    const box = within(dialog).getByRole('checkbox', { name: "Don't show this notice again" })
    expect(box).not.toBeChecked()
    expect(box.compareDocumentPosition(confirm) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(confirm)
    await expect(outcome).resolves.toBe(true)
    expect(localStorage.getItem(CREW_MODE_MIDCHAT_CONFIRM_DISMISSED_KEY)).toBeNull()
  })

  it('is shown once with the box ticked and confirmed, then skipped', async () => {
    const first = ask()
    const dialog = await screen.findByRole('dialog', { name: TITLE })
    fireEvent.click(within(dialog).getByRole('checkbox', { name: "Don't show this notice again" }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Turn on conductor mode' }))
    await expect(first).resolves.toBe(true)
    expect(localStorage.getItem(CREW_MODE_MIDCHAT_CONFIRM_DISMISSED_KEY)).toBe('1')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await expect(ask()).resolves.toBe(true)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('saves nothing on Cancel, even with the box ticked, and asks again next time', async () => {
    const first = ask()
    const dialog = await screen.findByRole('dialog', { name: TITLE })
    fireEvent.click(within(dialog).getByRole('checkbox', { name: "Don't show this notice again" }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await expect(first).resolves.toBe(false)
    expect(localStorage.getItem(CREW_MODE_MIDCHAT_CONFIRM_DISMISSED_KEY)).toBeNull()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    void ask()
    expect(await screen.findByRole('dialog', { name: TITLE })).toBeInTheDocument()
  })
})
