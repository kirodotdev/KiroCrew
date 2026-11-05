import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import KasEngineNotice from './KasEngineNotice'
import { api, type KasBundleStatus } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kirocrewConfig: vi.fn(),
      kasBundleStatus: vi.fn(),
      kasBundleInstall: vi.fn(),
    },
  }
})

const kirocrewConfig = vi.mocked(api.kirocrewConfig)
const kasBundleStatus = vi.mocked(api.kasBundleStatus)
const kasBundleInstall = vi.mocked(api.kasBundleInstall)

/**
 * Every banner sits behind two chained queries: the config read decides that KAS
 * is the backend, which enables the engine-status read the banner renders from.
 * Two round trips, so each wait names its own ceiling instead of the 1 s default.
 */
const CHAINED_QUERY_TIMEOUT = { timeout: 5_000 }

function status(over: Partial<KasBundleStatus> = {}): KasBundleStatus {
  return {
    enabled: false,
    pinned_version: '0.66.33',
    runs_version: '0.66.33',
    installed: null,
    installing: false,
    progress: { done: 0, total: 0 },
    error: '',
    route: null,
    ...over,
  }
}

describe('KasEngineNotice', () => {
  beforeEach(() => {
    kirocrewConfig.mockReset()
    kasBundleStatus.mockReset()
    kasBundleInstall.mockReset()
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } } as never)
  })

  it('says the engine is downloading while the gateway installs it', async () => {
    kasBundleStatus.mockResolvedValue(status({ installing: true, progress: { done: 25, total: 100 } }))
    renderWithProviders(<KasEngineNotice />)
    const notice = await screen.findByTestId('kas-engine-downloading', {}, CHAINED_QUERY_TIMEOUT)
    expect(notice.textContent).toMatch(/Downloading .*KAS engine/)
    expect(notice.textContent).toMatch(/25/)
  })

  it('says so when the engine status cannot be read', async () => {
    kasBundleStatus.mockRejectedValue(new Error('HTTP 500'))
    renderWithProviders(<KasEngineNotice />)
    const notice = await screen.findByTestId('kas-engine-status-error', {}, CHAINED_QUERY_TIMEOUT)
    expect(notice.textContent).toMatch(/Could not read the KAS engine status/)

  })

  it('keeps a dismissed status failure hidden while the same read keeps failing', async () => {
    kasBundleStatus.mockRejectedValue(new Error('HTTP 503'))
    const { queryClient } = renderWithProviders(<KasEngineNotice />)
    await screen.findByTestId('kas-engine-status-error', {}, CHAINED_QUERY_TIMEOUT)
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('kas-engine-status-error')).toBeNull()
    await queryClient.refetchQueries({ queryKey: ['kas-bundle'] })
    await waitFor(() => expect(kasBundleStatus.mock.calls.length).toBeGreaterThan(1), CHAINED_QUERY_TIMEOUT)
    expect(screen.queryByTestId('kas-engine-status-error')).toBeNull()
  })

  it('shows a failed download with its retry command and a Retry button', async () => {
    const error = 'Kiro Crew could not install its KAS engine: offline. Then retry: run `kirocrew kas install` in a terminal, or press Retry in the dashboard.'
    kasBundleStatus.mockResolvedValue(status({ error }))
    kasBundleInstall.mockResolvedValue(status({ installing: true }))
    renderWithProviders(<KasEngineNotice />)
    const notice = await screen.findByTestId('kas-engine-failed', {}, CHAINED_QUERY_TIMEOUT)
    expect(notice.textContent).toContain('kirocrew kas install')
    fireEvent.click(screen.getByTestId('kas-engine-retry'))
    await waitFor(() => expect(kasBundleInstall).toHaveBeenCalledTimes(1), CHAINED_QUERY_TIMEOUT)
    expect(await screen.findByTestId('kas-engine-downloading', {}, CHAINED_QUERY_TIMEOUT)).toBeTruthy()
  })

  it('says nothing once the engine is installed', async () => {
    kasBundleStatus.mockResolvedValue(status({
      installed: { version: '0.66.33', node_version: '22.23.3', bundled: false },
      error: 'stale',
    }))
    renderWithProviders(<KasEngineNotice />)
    await waitFor(() => expect(kasBundleStatus).toHaveBeenCalled(), CHAINED_QUERY_TIMEOUT)
    expect(screen.queryByTestId('kas-engine-failed')).toBeNull()
    expect(screen.queryByTestId('kas-engine-downloading')).toBeNull()
  })

  it('never reads the engine status when KAS is not the backend', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'claude' } } as never)
    renderWithProviders(<KasEngineNotice />)
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled())
    expect(kasBundleStatus).not.toHaveBeenCalled()
  })
})
