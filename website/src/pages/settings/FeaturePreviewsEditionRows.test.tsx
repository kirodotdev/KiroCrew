/**
 * Edition rows on Settings > Developer > Feature Previews: a row component
 * registered with `registerFeaturePreviewRows` renders in the section, its
 * switch calls the row's own handler, and a row that throws is isolated.
 */
import { useState } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { api } from '../../api/client'
import { SettingsCard, SettingsToggle } from '../../components/settings'
import { FeaturePreviewsSection } from './FeaturePreviewsSection'
import { registerFeaturePreviewRows, resetFeaturePreviewRowsForTests } from './featurePreviewRows'

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

const onToggle = vi.fn()

function SampleRow() {
  const [on, setOn] = useState(false)
  return (
    <SettingsCard>
      <SettingsToggle
        label="Sample preview"
        description="An edition feature."
        checked={on}
        onChange={v => { setOn(v); onToggle(v) }}
      />
    </SettingsCard>
  )
}

function BrokenRow(): never {
  throw new Error('row failed')
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
  it('renders a registered row component in Feature Previews', async () => {
    registerFeaturePreviewRows([{ id: 'sample', component: SampleRow }])
    mount()
    const sw = await screen.findByRole('switch', { name: 'Sample preview' })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText('An edition feature.')).toBeInTheDocument()
  })

  it("calls the row's own handler when its switch is toggled", async () => {
    registerFeaturePreviewRows([{ id: 'sample', component: SampleRow }])
    mount()
    fireEvent.click(await screen.findByRole('switch', { name: 'Sample preview' }))
    expect(onToggle).toHaveBeenCalledWith(true)
    expect(screen.getByRole('switch', { name: 'Sample preview' })).toHaveAttribute('aria-checked', 'true')
  })

  it('shows the crash card for a row that throws and keeps the other rows', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    registerFeaturePreviewRows([
      { id: 'broken', component: BrokenRow },
      { id: 'sample', component: SampleRow },
    ])
    mount()
    expect(await screen.findByRole('switch', { name: 'Sample preview' })).toBeInTheDocument()
    expect(screen.getByText('row failed')).toBeInTheDocument()
  })
})
