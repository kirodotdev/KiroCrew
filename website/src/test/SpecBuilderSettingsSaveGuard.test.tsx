// SettingsModal must not let a failed or pending settings read overwrite the
// stored path. basePath is seeded from the query, so before the read lands the
// buffer is still the initial '' -- and Save used to be guarded only by the SAVE
// mutation being in flight, leaving that window open.
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SettingsModal from '../apps/spec-builder/components/SettingsModal'
import { specApi } from '../apps/spec-builder/api'

function renderModal() {
  // retry:false so an isError state is reached immediately rather than after the
  // default retry schedule.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const setErr = vi.fn()
  render(
    <QueryClientProvider client={qc}>
      <SettingsModal onClose={() => {}} setErr={setErr} />
    </QueryClientProvider>,
  )
  return { setErr }
}

const saveButton = () => screen.getByRole('button', { name: /save/i })

describe('SettingsModal save guard', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('disables Save while the settings read is still pending', () => {
    // A promise that never settles: the read is in flight for the whole test.
    vi.spyOn(specApi, 'getSettings').mockReturnValue(new Promise(() => {}) as never)
    renderModal()
    expect(saveButton()).toBeDisabled()
  })

  it('keeps Save disabled after the settings read fails, and reports why', async () => {
    vi.spyOn(specApi, 'getSettings').mockRejectedValue(new Error('settings unavailable'))
    const saveSpy = vi.spyOn(specApi, 'saveSettings')
    const { setErr } = renderModal()

    await waitFor(() => expect(saveButton()).toBeDisabled())
    // The failure is surfaced, so the disabled control is not unexplained.
    await waitFor(() => expect(setErr).toHaveBeenCalledWith('settings unavailable'))
    // And the destructive write never becomes reachable.
    expect(saveSpy).not.toHaveBeenCalled()
  })

  it('enables Save once the read lands, with the stored path seeded', async () => {
    vi.spyOn(specApi, 'getSettings').mockResolvedValue({ base_path: '/srv/specs' })
    renderModal()

    await waitFor(() => expect(saveButton()).toBeEnabled())
    expect(screen.getByDisplayValue('/srv/specs')).toBeInTheDocument()
  })
})
