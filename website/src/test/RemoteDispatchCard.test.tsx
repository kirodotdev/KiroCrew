/**
 * RemoteDispatchCard — Settings > Developer > Feature Previews: conductor remote dispatch.
 *
 * What is pinned:
 * - the switch reads `work_ledger.remote_dispatch` from the gateway config and is
 *   OFF unless that value is a literal `true`;
 * - a toggle writes `work_ledger.remote_dispatch` through the ordinary config PATCH;
 * - a gateway whose config does not carry the key gets no card, since the write
 *   would only fail;
 * - a failed save says so, and a failed config read shows a notice instead of
 *   vanishing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { RemoteDispatchCard } from '../pages/settings/RemoteDispatchCard'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kirocrewConfig: vi.fn(),
      patchConfig: vi.fn().mockResolvedValue({}),
    },
  }
})

const kirocrewConfig = vi.mocked(api.kirocrewConfig)
const patchConfig = vi.mocked(api.patchConfig)

describe('RemoteDispatchCard', () => {
  beforeEach(() => {
    kirocrewConfig.mockReset()
    patchConfig.mockReset()
    patchConfig.mockResolvedValue({} as never)
  })

  it('is off by default and writes the gateway switch when turned on', async () => {
    kirocrewConfig.mockResolvedValue({ work_ledger: { remote_dispatch: false } } as never)
    renderWithProviders(<RemoteDispatchCard />)
    const sw = await screen.findByRole('switch')
    expect(sw).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('work_ledger.remote_dispatch', true))
  })

  it('shows the stored ON and turns it off', async () => {
    kirocrewConfig.mockResolvedValue({ work_ledger: { remote_dispatch: true } } as never)
    renderWithProviders(<RemoteDispatchCard />)
    const sw = await screen.findByRole('switch')
    expect(sw).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('work_ledger.remote_dispatch', false))
  })

  it('draws nothing for a gateway that does not carry the key', async () => {
    kirocrewConfig.mockResolvedValue({ work_ledger: {} } as never)
    renderWithProviders(<RemoteDispatchCard />)
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled())
    expect(screen.queryByTestId('remote-dispatch-card')).toBeNull()
  })

  it('says so when the save fails', async () => {
    kirocrewConfig.mockResolvedValue({ work_ledger: { remote_dispatch: false } } as never)
    patchConfig.mockRejectedValue(new Error('nope'))
    renderWithProviders(<RemoteDispatchCard />)
    fireEvent.click(await screen.findByRole('switch'))
    expect(await screen.findByText('Could not save remote dispatch. Try again.')).toBeInTheDocument()
  })

  it('says so when the config read fails instead of hiding the switch', async () => {
    kirocrewConfig.mockRejectedValue(new Error('down'))
    renderWithProviders(<RemoteDispatchCard />)
    expect(await screen.findByText('Could not read the remote dispatch setting.', {}, { timeout: 10000 })).toBeInTheDocument()
    expect(screen.queryByRole('switch')).toBeNull()
  })
})
