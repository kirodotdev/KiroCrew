/**
 * Save is GLOBAL but both of its failure messages are section-scoped: the
 * refused-shortcut notice renders only under `active === 'shortcuts'` and the
 * failed-instance-switch notice only under `active === 'instances'`. Both
 * failures also `return` early to hold the panel open.
 *
 * So without an explicit reveal, saving from any other section produces a panel
 * that refuses to close and states no reason — the message exists, two clicks
 * away, in a section the user is not looking at. These tests drive the real
 * component through the real Save handler and assert the message is ON SCREEN,
 * which is the only thing that distinguishes "explained" from "silently stuck".
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

const setPetInstance = vi.fn()
const applyShortcuts = vi.fn()
/** Flipped per test; the mock reads it through a getter so it stays live. */
let hasShell = true

// Both the panel and the vendored instance list import this same module.
vi.mock('../src/mochiApi', () => ({
  api: {
    // Default to the desktop case; the no-shell tests override it.
    get hasShell() {
      return hasShell
    },
    getConfig: async () => ({
      mochi: { petName: 'Mochi', petInstance: 'self' },
      shortcuts: {
        toggleWindow: 'CommandOrControl+Shift+M',
        // All three: revealing the Shortcuts section renders a field per key,
        // and displayAccelerator() splits the string, so a missing key throws
        // during render and React tears down the whole tree.
        screenCapture: 'CommandOrControl+Shift+S',
        hideAll: 'CommandOrControl+Shift+H',
      },
      window: { chatAlwaysOnTop: true },
    }),
    getMochiTrustLevel: async () => 'default',
    getStats: async () => ({}),
    instancesList: async () => ({
      state: 'ready',
      instances: [
        // isUsable() requires a live port AND a connected tunnel, or the row
        // renders un-pickable and the switch path is never reached.
        { id: 'self', name: 'This instance', local_port: 5476, status: { state: 'connected' } },
        { id: 'remote-1', name: 'Remote crew', local_port: 5477, status: { state: 'connected' } },
      ],
    }),
    updateConfig: async () => undefined,
    setMochiTrustLevel: async () => undefined,
    applyShortcuts,
    setPetInstance,
    onSettingsCloseRequested: () => () => {},
  },
}))

const { SettingsPanel } = await import('../src/renderer/SettingsPanel')

describe('SettingsPanel Save reveals the section that explains a failure', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    hasShell = true
    // The default happy path for whichever half a given test is not exercising.
    applyShortcuts.mockResolvedValue({})
    setPetInstance.mockResolvedValue(true)
  })

  it('jumps to Instances when the instance switch fails from another section', async () => {
    const user = userEvent.setup()
    setPetInstance.mockResolvedValue(false)
    const onClose = vi.fn()
    render(<SettingsPanel onClose={onClose} />)

    // Pick a different instance, which is only possible from Instances.
    await user.click(await screen.findByText('Instances'))
    await user.click(await screen.findByText('Remote crew'))

    // Then walk away from that section before saving — the whole point.
    await user.click(screen.getByText('General'))
    await waitFor(() =>
      expect(screen.queryByText(/Could not switch instance/)).not.toBeInTheDocument(),
    )

    await user.click(screen.getByRole('button', { name: 'Save' }))

    // The message is visible, so the held-open panel explains itself.
    expect(await screen.findByText(/Could not switch instance/)).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('commits the baseline on a failed switch, so Discard cannot un-move the pet', async () => {
    const user = userEvent.setup()
    // The shell writes the pointer BEFORE the reconcile that fails, so the choice
    // is persisted and its retry loop will apply it. If the panel still treated
    // the old value as the baseline it would offer a revert it cannot perform,
    // and the pet would move anyway after the user "discarded" it.
    setPetInstance.mockResolvedValue(false)
    render(<SettingsPanel onClose={vi.fn()} />)

    await user.click(await screen.findByText('Instances'))
    await user.click(await screen.findByText('Remote crew'))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/Could not switch instance/)).toBeInTheDocument()

    // Save disabled == nothing left to save == no revert is being advertised.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled())
  })

  it('jumps to Shortcuts when a key is refused from another section', async () => {
    const user = userEvent.setup()
    // applyShortcuts runs on EVERY save, so no shortcut editing is needed to
    // reach the refusal branch.
    applyShortcuts.mockResolvedValue({ toggleWindow: false })
    const onClose = vi.fn()
    render(<SettingsPanel onClose={onClose} />)

    // Save is disabled until something is dirty, so nudge the pet name. Stays
    // in General — the section that explains nothing about shortcuts.
    await user.type(await screen.findByDisplayValue('Mochi'), '!')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText(/Another app already owns this key/)).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })
})

/**
 * Both panes hold a per-MACHINE preference the Electron shell's store owns, and
 * neither is persisted by the gateway — the accelerators are dropped by
 * `flattenConfig` outright. So without a shell the only honest thing either pane
 * can do is say so.
 *
 * The gate must be `api.hasShell` and NOT the presence of a shell-backed method:
 * `mochiApi` assigns `setPetInstance` / `machinePrefs` / `instancesList`
 * unconditionally from petBridge, so they are defined in a browser tab too and
 * merely resolve falsey. A method-presence check silently never fires.
 */
describe('SettingsPanel hides shell-only controls when there is no shell', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    hasShell = false
    applyShortcuts.mockResolvedValue({})
    setPetInstance.mockResolvedValue(false)
  })

  it('replaces the instance picker with the desktop-app note', async () => {
    const user = userEvent.setup()
    render(<SettingsPanel onClose={vi.fn()} />)
    await user.click(await screen.findByText('Instances'))

    expect(
      await screen.findByText(/Choosing which instance the pet shows needs the desktop app/),
    ).toBeInTheDocument()
    // The picker itself must be gone, not merely accompanied by a note.
    expect(screen.queryByText('Remote crew')).not.toBeInTheDocument()
  })

  it('replaces the shortcut editors with the desktop-app note', async () => {
    const user = userEvent.setup()
    render(<SettingsPanel onClose={vi.fn()} />)
    await user.click(await screen.findByText('Shortcuts'))

    expect(
      await screen.findByText(/Changing the global shortcuts needs the desktop app/),
    ).toBeInTheDocument()
    // No editable accelerator: a save here would report success and persist
    // nothing, which is the regression this gate exists to prevent.
    expect(screen.queryByText(/Screen capture|Toggle window/)).not.toBeInTheDocument()
  })
})
