/**
 * Settings > Developer > Feature previews: the "Kiro Managed Environments" row.
 *
 * It shows where the gateway says the lane's sign-in values are set
 * (`managed_env_available` on the config read) or the stored switch is on, and
 * never under the LTS lock.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../api/client'
import { __resetPreviewFlagServerSyncForTests, usePreviewFlagServerSync } from '../hooks/usePreviewFlagServerSync'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'
import { applyLockedControls, LOCKED_FEATURE_PREVIEWS } from '../utils/editionLocks'

const NAME = 'Kiro Managed Environments'

function WithSync() {
  usePreviewFlagServerSync()
  return <FeaturePreviewsSection />
}

function renderSection(available: boolean | undefined, featurePreviews: Record<string, boolean> = {}) {
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({
    dashboard: { feature_previews: featurePreviews },
    ...(available === undefined ? {} : { managed_env_available: available }),
  } as never)
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })}>
      <MemoryRouter initialEntries={['/settings']}>
        <WithSync />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  applyLockedControls([])
  __resetPreviewFlagServerSyncForTests()
  vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
  vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue({
    enabled: false, endpoint: '', configured_endpoint: 'https://api.typesafe.ai/v1/systemone', permits: false,
  })
})

afterEach(() => {
  cleanup()
  act(() => { __resetPreviewFlagServerSyncForTests() })
  applyLockedControls([])
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('Feature previews: Kiro Managed Environments', () => {
  it('is hidden when the sign-in values are not set', async () => {
    renderSection(false)
    await waitFor(() => expect(screen.getByRole('switch', { name: 'KAS sub-agents' })).toBeInTheDocument())
    expect(screen.queryByRole('switch', { name: NAME })).toBeNull()
  })

  it('is hidden on a gateway that does not report it', async () => {
    renderSection(undefined)
    await waitFor(() => expect(screen.getByRole('switch', { name: 'KAS sub-agents' })).toBeInTheDocument())
    expect(screen.queryByRole('switch', { name: NAME })).toBeNull()
  })

  it('shows its switch, off by default, when the sign-in values are set', async () => {
    renderSection(true)
    const toggle = await screen.findByRole('switch', { name: NAME })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText('Adds Kiro-managed remote machines to Add Crew in Settings > Remote Crew. Turning this on opens none until you launch one.')).toBeInTheDocument()
  })

  it('shows on a plain install while the stored switch is on, and turning it off hides it', async () => {
    // The gateway keeps what it is sent, so the refetch after the write agrees with it.
    const stored: Record<string, boolean> = { crew_client: true }
    vi.spyOn(api, 'patchConfig').mockImplementation(async (_path, value) => {
      stored.crew_client = value === true
      return {} as never
    })
    renderSection(false, stored)
    const toggle = await screen.findByRole('switch', { name: NAME })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    await act(async () => { toggle.click() })
    await waitFor(() => expect(screen.queryByRole('switch', { name: NAME })).toBeNull())
  })

  it('stays hidden under the LTS lock even when the values are set', async () => {
    applyLockedControls([LOCKED_FEATURE_PREVIEWS])
    renderSection(true)
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    // Let the config read settle before checking the row is still absent.
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByRole('switch', { name: NAME })).toBeNull()
  })
})
