/**
 * Settings → Shortcuts, Desktop App: the summon-hotkey recorder.
 *
 * A window on this machine's own gateway records the system-wide summon hotkey
 * here, or clears it to turn it off, and the desktop shell binds the change at
 * once. A window that gets no setter from the shell (connected to a remote
 * host, or an older shell) keeps the read-only row and the config-file hint.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'

// The shell flag is read at module load; force the desktop branch.
vi.mock('../../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/electron')>()),
  isElectron: true,
}))

import { renderWithProviders } from '../../test/helpers'
import { ShortcutsPanel } from './ShortcutsPanel'

type HotkeyInfo = { accelerator: string; requested: string; default: string }
type BridgeWindow = Window & {
  electronAPI?: { getGlobalHotkey?: () => Promise<HotkeyInfo>; setGlobalHotkey?: (accelerator: string) => Promise<HotkeyInfo> }
}

const OFF: HotkeyInfo = { accelerator: '', requested: '', default: 'Alt+Shift+K' }
const bound = (accelerator: string): HotkeyInfo => ({ accelerator, requested: accelerator, default: 'Alt+Shift+K' })

/** The summon row: its label, the record button and Clear share one parent. */
async function summonRow(): Promise<HTMLElement> {
  const label = await screen.findByText(/Show or focus/)
  return label.parentElement as HTMLElement
}
const recordButton = (row: HTMLElement) => within(row).getByRole('button', { name: 'Press keys…' })

afterEach(() => {
  delete (window as BridgeWindow).electronAPI
})

describe('Settings → Shortcuts: summon-hotkey recorder', () => {
  it('records a chord for a hotkey that is off, then clears it again', async () => {
    const set = vi.fn(async (accelerator: string) => (accelerator ? bound(accelerator) : OFF))
    ;(window as BridgeWindow).electronAPI = { getGlobalHotkey: async () => OFF, setGlobalHotkey: set }
    renderWithProviders(<ShortcutsPanel />)
    const row = await summonRow()

    // Off: nothing to clear, nothing that "always works".
    expect(within(row).queryByRole('button', { name: 'Clear' })).toBeNull()
    expect(within(row).queryByText('always works')).toBeNull()
    expect(screen.getByText(/other apps cannot use the same keys/)).toBeInTheDocument()
    expect(screen.queryByText(/desktop config file/)).toBeNull()

    fireEvent.click(recordButton(row))
    fireEvent.keyDown(window, { key: 'J', code: 'KeyJ', altKey: true, shiftKey: true })
    expect(set).toHaveBeenCalledExactlyOnceWith('Alt+Shift+J')

    // The button shows what the shell bound, tagged: "Enable shortcuts" does not turn it off.
    await waitFor(() => expect(recordButton(row)).toHaveTextContent('Alt+Shift+J'))
    expect(within(row).getByText('always works')).toBeInTheDocument()

    fireEvent.click(within(row).getByRole('button', { name: 'Clear' }))
    expect(set).toHaveBeenLastCalledWith('')
    await waitFor(() => expect(within(row).queryByRole('button', { name: 'Clear' })).toBeNull())
    expect(recordButton(row)).toHaveTextContent('Press keys…')
  })

  it('says so when the recorded chord could not be bound, and Clear drops it', async () => {
    // The shell stored the chord, could not register it, and bound its default.
    const fellBack: HotkeyInfo = { accelerator: 'Alt+Shift+K', requested: 'Ctrl+Alt+J', default: 'Alt+Shift+K' }
    const set = vi.fn(async (accelerator: string) => (accelerator ? fellBack : OFF))
    ;(window as BridgeWindow).electronAPI = { getGlobalHotkey: async () => OFF, setGlobalHotkey: set }
    renderWithProviders(<ShortcutsPanel />)
    const row = await summonRow()

    fireEvent.click(recordButton(row))
    fireEvent.keyDown(window, { key: 'j', code: 'KeyJ', ctrlKey: true, altKey: true })
    expect(await screen.findByText(/so the default is used instead/)).toBeInTheDocument()
    expect(recordButton(row)).toHaveTextContent('Alt+Shift+K')

    fireEvent.click(within(row).getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(screen.queryByText(/so the default is used instead/)).toBeNull())
  })

  it('shows an error and keeps the hotkey when the shell refuses the change', async () => {
    const set = vi.fn(async () => { throw new Error('global-hotkey:set is restricted to the local dashboard') })
    ;(window as BridgeWindow).electronAPI = { getGlobalHotkey: async () => bound('Alt+Shift+K'), setGlobalHotkey: set }
    renderWithProviders(<ShortcutsPanel />)
    const row = await summonRow()

    fireEvent.click(within(row).getByRole('button', { name: 'Clear' }))
    expect(await screen.findByTestId('global-hotkey-save-error')).toHaveTextContent("Couldn't change the shortcut")
    expect(recordButton(row)).toHaveTextContent('Alt+Shift+K')
  })

  it('keeps the read-only row and the config-file hint where the shell gives no setter', async () => {
    ;(window as BridgeWindow).electronAPI = { getGlobalHotkey: async () => OFF }
    renderWithProviders(<ShortcutsPanel />)
    const row = await summonRow()

    expect(within(row).queryByRole('button')).toBeNull()
    expect(within(row).getByText('Off')).toBeInTheDocument()
    expect(screen.getByText(/desktop config file/)).toBeInTheDocument()
    expect(screen.queryByText(/other apps cannot use the same keys/)).toBeNull()
  })
})
