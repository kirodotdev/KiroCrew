/**
 * The md-notebook Settings "Save" token button keeps keyboard focus while its
 * request runs. A native `disabled` button leaves the tab order, so a keyboard
 * user who pressed it would lose focus to the page body until the request
 * settled. The button now uses `aria-disabled` plus a busy guard instead, and
 * ignores a second press while the first request is in flight.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'

vi.mock('../apps/md-notebook/api', () => ({
  notesApi: {},
  knowledgeRegister: vi.fn(),
  knowledgeUnregister: vi.fn(),
}))

const { SettingsPage } = await import('../apps/md-notebook/SettingsPage')

const baseProps = {
  vaults: [],
  activeVaultId: null,
  hasPat: false,
  hasGhAuth: false,
  autoSync: false,
  autoSyncMins: 5,
  autoCommit: false,
  syncPrefsError: null,
  shortcut: { key: 's', mod: true } as const,
  onClose: vi.fn(),
  onSwitchVault: vi.fn(),
  onConnect: vi.fn(),
  onForget: vi.fn(async () => {}),
  onSetKnowledge: vi.fn(async () => {}),
  onAutoSync: vi.fn(),
  onAutoSyncMins: vi.fn(),
  onAutoCommit: vi.fn(),
  onSetShortcut: vi.fn(),
  onRecordingChange: vi.fn(),
}

beforeEach(() => {
  cleanup()
})

describe('md-notebook/SettingsPage busy focus', () => {
  it('keeps focus on the Save token button while it runs and ignores a second press', async () => {
    let settle: () => void = () => {}
    const onSetPat = vi.fn(() => new Promise<void>(resolve => { settle = resolve }))
    render(<SettingsPage {...baseProps} onSetPat={onSetPat} />)

    // Type a token so the field-empty native disable clears and the button is
    // reachable; find it by its accessible name.
    const input = screen.getByLabelText('Access token (optional)') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'zz-token' } })

    const button = screen.getByRole('button', { name: 'Save' })
    button.focus()
    fireEvent.click(button)

    // While busy the button is aria-disabled, NOT natively disabled, so it
    // stays in the tab order and keeps focus.
    await waitFor(() => expect(button).toHaveAttribute('aria-disabled', 'true'))
    expect(button).not.toBeDisabled()
    expect(button).toHaveFocus()

    // A second press while busy is ignored.
    fireEvent.click(button)

    settle()
    await waitFor(() => expect(button).not.toHaveAttribute('aria-disabled'))
    // Counted after the save settles, so a second save started by the second
    // press would already show here.
    expect(onSetPat).toHaveBeenCalledTimes(1)
  })
})
