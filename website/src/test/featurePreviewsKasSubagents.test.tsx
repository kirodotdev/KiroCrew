/**
 * Settings > Developer > Feature previews: the "KAS sub-agents" card.
 *
 * A title, one line and the switch, off by default. Its switch is the gateway's
 * `dashboard.feature_previews.kas_subagents`, the one stored setting that picks
 * the engine's sub-agents for a KAS chat; an LTS build shows no card.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../api/client'
import { __resetPreviewFlagServerSyncForTests, usePreviewFlagServerSync } from '../hooks/usePreviewFlagServerSync'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'
import { PREVIEW_KAS_SUBAGENTS, previewFlagConfigPath } from '../utils/previewFlags'
import { applyLockedControls, LOCKED_FEATURE_PREVIEWS } from '../utils/editionLocks'

// One sentence: the info tip beside it carries what each part means.
const LINE = 'On KAS, the default agent runs quick sub-agents inside the engine, and some agent permissions are read the way kiro-cli reads them, in chats and sub-agents started after you switch this.'

function WithSync() {
  usePreviewFlagServerSync()
  return <FeaturePreviewsSection />
}

function renderSection() {
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
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: { feature_previews: {} } } as never)
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

describe('Feature previews: KAS sub-agents', () => {
  it('is a title, one line and a switch that reads off by default', async () => {
    renderSection()
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    const toggle = screen.getByRole('switch', { name: 'KAS sub-agents' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText(LINE)).toBeInTheDocument()
    const row = toggle.closest('[data-settings-row]') ?? toggle.parentElement!.parentElement!
    expect(within(row as HTMLElement).queryByRole('button', { name: /see what it looks like/i })).toBeNull()
    expect(within(row as HTMLElement).queryByRole('img')).toBeNull()
  })

  it('sends each toggle to the gateway as the one stored setting', async () => {
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    renderSection()
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    await act(async () => { screen.getByRole('switch', { name: 'KAS sub-agents' }).click() })
    await waitFor(() => expect(patch).toHaveBeenCalledWith(previewFlagConfigPath(PREVIEW_KAS_SUBAGENTS), true))
    expect(previewFlagConfigPath(PREVIEW_KAS_SUBAGENTS)).toBe('dashboard.feature_previews.kas_subagents')
  })

  it('shows no card when the edition locks Feature previews', async () => {
    applyLockedControls([LOCKED_FEATURE_PREVIEWS])
    renderSection()
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    expect(screen.queryByRole('switch', { name: 'KAS sub-agents' })).toBeNull()
  })
})
