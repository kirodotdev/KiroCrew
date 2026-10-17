// SettingsModal failures must render INSIDE the dialog. The page-top banner the
// caller owns sits behind the modal's dimmed backdrop while focus is trapped in
// the dialog, so a failed save read as the Save button silently reverting and a
// failed read left Save disabled with no visible reason.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SettingsModal from '../apps/spec-builder/components/SettingsModal'
import { specApi } from '../apps/spec-builder/api'

vi.mock('../hooks/useAvailableModels', () => ({
  useAvailableModels: () => [{ name: 'test-model-x', description: '' }],
}))

function renderModal(qc: QueryClient, onClose = () => {}) {
  return render(
    <QueryClientProvider client={qc}>
      <SettingsModal onClose={onClose} />
    </QueryClientProvider>,
  )
}

const newClient = () => new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
})

const dialog = () => screen.getByRole('dialog', { name: 'Settings' })
const saveButton = () => screen.getByRole('button', { name: /^save$/i })

describe('SettingsModal error surface', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('renders a failed save inside the dialog and keeps it open', async () => {
    vi.spyOn(specApi, 'getSettings').mockResolvedValue({ base_path: '/srv/specs', model: '' })
    vi.spyOn(specApi, 'saveSettings').mockRejectedValue(new Error('settings file is read-only'))
    const onClose = vi.fn()
    renderModal(newClient(), onClose)

    await waitFor(() => expect(saveButton()).toBeEnabled())
    fireEvent.click(saveButton())

    // Translated lead + the raw reason, both inside the dialog the user is in.
    const alert = await within(dialog()).findByRole('alert')
    expect(alert).toHaveTextContent('Couldn’t save these settings.')
    expect(alert).toHaveTextContent('settings file is read-only')
    // The modal only closes on success; the failure must not read as a close.
    expect(onClose).not.toHaveBeenCalled()
    expect(dialog()).toBeInTheDocument()
    // Save stays reachable for a retry once the write has settled.
    await waitFor(() => expect(saveButton()).toBeEnabled())
  })

  it('cannot be dismissed while the save is pending, so a late failure still lands', async () => {
    vi.spyOn(specApi, 'getSettings').mockResolvedValue({ base_path: '/srv/specs', model: '' })
    let reject: (e: Error) => void = () => {}
    vi.spyOn(specApi, 'saveSettings').mockReturnValue(new Promise((_, r) => { reject = r }) as never)
    const onClose = vi.fn()
    renderModal(newClient(), onClose)

    await waitFor(() => expect(saveButton()).toBeEnabled())
    fireEvent.click(saveButton())
    await waitFor(() => expect(screen.getByRole('button', { name: /saving/i })).toBeDisabled())

    // Every dismissal path is refused while the write is in flight, and the
    // refusal is visible on both explicit controls.
    expect(screen.getByRole('button', { name: /^cancel$/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /close/i })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }))
    fireEvent.click(screen.getByRole('button', { name: /close/i }))
    fireEvent.keyDown(dialog(), { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()

    // The write settles late, and the failure has a mounted dialog to land in.
    reject(new Error('settings file is read-only'))
    const alert = await within(dialog()).findByRole('alert')
    expect(alert).toHaveTextContent('settings file is read-only')
    // Dismissal is available again once the write has settled.
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('renders a failed read inside the dialog, explaining the disabled Save', async () => {
    vi.spyOn(specApi, 'getSettings').mockRejectedValue(new Error('settings unavailable'))
    renderModal(newClient())

    const alert = await within(dialog()).findByRole('alert')
    expect(alert).toHaveTextContent('Couldn’t load these settings, so saving is off until they load.')
    expect(alert).toHaveTextContent('settings unavailable')
    expect(saveButton()).toBeDisabled()
  })

  it('opens a fresh dialog without the previous attempt’s save failure', async () => {
    vi.spyOn(specApi, 'getSettings').mockResolvedValue({ base_path: '/srv/specs', model: '' })
    vi.spyOn(specApi, 'saveSettings').mockRejectedValue(new Error('settings file is read-only'))
    // One QueryClient across both opens, exactly as the page shares its client
    // with every SettingsModal it mounts.
    const qc = newClient()
    const first = renderModal(qc)

    await waitFor(() => expect(saveButton()).toBeEnabled())
    fireEvent.click(saveButton())
    await within(dialog()).findByRole('alert')

    // Cancel: the page unmounts the modal.
    first.unmount()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    // Reopen: the failure belonged to a dialog the user already dismissed.
    renderModal(qc)
    await waitFor(() => expect(saveButton()).toBeEnabled())
    expect(within(dialog()).queryByRole('alert')).not.toBeInTheDocument()
  })
})
