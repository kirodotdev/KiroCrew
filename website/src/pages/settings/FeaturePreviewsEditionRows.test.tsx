/**
 * Edition rows on Settings > Developer > Feature Previews: a row registered with
 * `registerFeaturePreviewRows` renders in the section, and its switch writes its
 * config key through `/api/feature-previews/edition`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { FeaturePreviewsSection } from './FeaturePreviewsSection'
import { registerFeaturePreviewRows, resetFeaturePreviewRowsForTests, type FeaturePreviewRowDef } from './featurePreviewRows'

vi.mock('../../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      featurePreviewEditionValues: vi.fn(),
      setFeaturePreviewEditionValue: vi.fn(),
      sharedCrewPreview: vi.fn(),
      dashboardConfig: vi.fn(),
      kirocrewConfig: vi.fn(),
      getDecisionsConsent: vi.fn(),
    },
  }
})
const read = vi.mocked(api.featurePreviewEditionValues)
const write = vi.mocked(api.setFeaturePreviewEditionValue)

const ROW: FeaturePreviewRowDef = {
  id: 'sample',
  label: 'Sample preview',
  description: 'An edition feature.',
  configKey: 'sample_edition.enabled',
  defaultValue: false,
  appliesOnRestart: true,
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><FeaturePreviewsSection /></MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.resetAllMocks()
  resetFeaturePreviewRowsForTests()
  vi.mocked(api.sharedCrewPreview).mockResolvedValue(null)
  vi.mocked(api.dashboardConfig).mockResolvedValue({ decisions_enabled: false } as never)
  vi.mocked(api.kirocrewConfig).mockResolvedValue({} as never)
  vi.mocked(api.getDecisionsConsent).mockResolvedValue({ enabled: false, endpoint: '', configured_endpoint: '', permits: false })
})

afterEach(() => resetFeaturePreviewRowsForTests())

describe('edition Feature Preview rows', () => {
  it('renders a registered row at its default while the key is unset', async () => {
    registerFeaturePreviewRows([ROW])
    read.mockResolvedValue({ values: { 'sample_edition.enabled': null } })
    mount()
    const sw = await screen.findByRole('switch', { name: 'Sample preview' })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText('An edition feature.')).toBeInTheDocument()
  })

  it('writes its config key on toggle and says a restart applies it', async () => {
    registerFeaturePreviewRows([ROW])
    read.mockResolvedValue({ values: { 'sample_edition.enabled': null } })
    write.mockResolvedValue({ config_key: 'sample_edition.enabled', enabled: true })
    mount()
    fireEvent.click(await screen.findByRole('switch', { name: 'Sample preview' }))
    await waitFor(() => expect(write).toHaveBeenCalledWith('sample_edition.enabled', true))
    await waitFor(() =>
      expect(screen.getByRole('switch', { name: 'Sample preview' })).toHaveAttribute('aria-checked', 'true'))
    expect(screen.getByTestId('edition-preview-row-sample-restart')).toBeInTheDocument()
  })

  it('does not draw a row whose key the gateway does not accept', async () => {
    registerFeaturePreviewRows([ROW])
    read.mockResolvedValue({ values: {} })
    mount()
    await waitFor(() => expect(read).toHaveBeenCalled())
    expect(screen.queryByTestId('edition-preview-row-sample')).toBeNull()
  })

  it('says a failed read failed, rather than hiding the rows', async () => {
    registerFeaturePreviewRows([ROW])
    read.mockRejectedValue(new Error('network down'))
    mount()
    expect(await screen.findByTestId('edition-preview-rows-load-error')).toBeInTheDocument()
  })

  it('renders nothing for a gateway without the route or a caller who is not the owner', async () => {
    registerFeaturePreviewRows([ROW])
    read.mockRejectedValue(new ApiError(403, 'Forbidden', ''))
    mount()
    await waitFor(() => expect(read).toHaveBeenCalled())
    expect(screen.queryByTestId('edition-preview-rows-load-error')).toBeNull()
    expect(screen.queryByTestId('edition-preview-row-sample')).toBeNull()
  })

  it('fetches nothing when no row is registered', async () => {
    mount()
    await screen.findAllByRole('switch')
    expect(read).not.toHaveBeenCalled()
  })
})
