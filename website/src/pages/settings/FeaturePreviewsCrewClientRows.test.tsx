/**
 * The Remote Crew client rows on Settings > Developer > Feature Previews: both
 * switches start off, and each one writes its own preview flag.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { api } from '../../api/client'
import { PREVIEW_CREW_CLIENT, PREVIEW_LAUNCH_CARD_CLOSE, readPreviewFlag } from '../../utils/previewFlags'
import { FeaturePreviewsSection } from './FeaturePreviewsSection'

vi.mock('../../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      sharedCrewPreview: vi.fn(),
      dashboardConfig: vi.fn(),
      kirocrewConfig: vi.fn(),
      getDecisionsConsent: vi.fn(),
    },
  }
})

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
  localStorage.clear()
  vi.mocked(api.sharedCrewPreview).mockResolvedValue(null)
  vi.mocked(api.dashboardConfig).mockResolvedValue({ decisions_enabled: false } as never)
  vi.mocked(api.kirocrewConfig).mockResolvedValue({} as never)
  vi.mocked(api.getDecisionsConsent).mockResolvedValue({ enabled: false, endpoint: '', configured_endpoint: '', permits: false })
})

describe('Remote Crew client preview rows', () => {
  it.each([
    ["Use your company's sign-in", PREVIEW_CREW_CLIENT],
    ['Close finished Remote Crew setup cards', PREVIEW_LAUNCH_CARD_CLOSE],
  ])('%s starts off and turns its own flag on', async (label, flag) => {
    mount()
    const sw = await screen.findByRole('switch', { name: label })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(sw)
    expect(readPreviewFlag(flag)).toBe(true)
    expect(screen.getByRole('switch', { name: label })).toHaveAttribute('aria-checked', 'true')
  })
})
