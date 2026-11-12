/**
 * GlobalHotkeyRow — the summon-hotkey row says so when the bound chord is not
 * the one the stored preference asked for (fell back to the default, or nothing
 * could be registered), and stays quiet when the user turned it off or the
 * desktop shell is too old to report what was asked for.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderWithProviders } from '../test/helpers'
import { __resetNavSeamForTests, installSoftNavigate } from '../utils/errorReport'

// The shell flag is read at module load; force the desktop branch.
vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  isElectron: true,
}))

import ShortcutsModal, { GlobalHotkeyRow } from './ShortcutsModal'

type BridgeWindow = Window & { electronAPI?: { getGlobalHotkey?: () => Promise<unknown> } }

afterEach(() => {
  delete (window as BridgeWindow).electronAPI
  __resetNavSeamForTests()
})

describe('GlobalHotkeyRow', () => {
  it.each([
    ['fell back to the default', { accelerator: 'Alt+Shift+K', requested: 'Ctrl+Shift+J' }, /so the default is used instead/],
    ['could not be registered', { accelerator: '', requested: 'Alt+Shift+K' }, /Another app may already be using it/],
    ['is bound as asked', { accelerator: 'Alt+Shift+K', requested: 'Alt+Shift+K' }, null],
    ['is turned off', { accelerator: '', requested: '' }, null],
    ['comes from an older shell', { accelerator: '' }, null],
  ])('a summon hotkey that %s', async (_case, info, notice) => {
    ;(window as BridgeWindow).electronAPI = {
      getGlobalHotkey: async () => ({ ...info, default: 'Alt+Shift+K' }),
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><GlobalHotkeyRow /></QueryClientProvider>)
    // The label and the notice come from the same query result, so one wait covers both.
    expect(await screen.findByText(/Show or focus/)).toBeInTheDocument()
    if (notice) expect(screen.getByText(notice)).toBeInTheDocument()
    else expect(screen.queryByText(/Couldn't register/)).toBeNull()
  })

  it('closes the shortcuts modal when the notice hands off to the chat', async () => {
    installSoftNavigate(() => {})
    ;(window as BridgeWindow).electronAPI = {
      getGlobalHotkey: async () => ({ accelerator: '', requested: 'Alt+Shift+K', default: 'Alt+Shift+K' }),
    }
    const onClose = vi.fn()
    renderWithProviders(<ShortcutsModal onClose={onClose} />)
    const notice = await screen.findByTestId('global-hotkey-notice')
    fireEvent.click(within(notice).getByRole('button', { name: /ask the agent/i }))
    expect(onClose).toHaveBeenCalledOnce()
  })
})
