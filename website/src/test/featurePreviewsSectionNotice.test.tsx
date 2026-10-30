/**
 * The Feature previews section with the gateway sync mounted, as the app shell
 * mounts it: every card's switch hands its toggle to the gateway, a refused
 * write shows one notice that names the switch that went back, and each
 * ingress link opens its page.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

import { api } from '../api/client'
import {
  PREVIEW_FLAGS_SYNCED_KEY, __resetPreviewFlagServerSyncForTests, usePreviewFlagServerSync,
} from '../hooks/usePreviewFlagServerSync'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'
import { PREVIEW_ARTIFACT_DEPLOY, PREVIEW_WEBHOOKS } from '../utils/previewFlags'

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>
}

function WithSync() {
  usePreviewFlagServerSync()
  return <FeaturePreviewsSection />
}

function renderSection() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })}>
      <MemoryRouter initialEntries={['/settings']}>
        <Routes>
          <Route path="/settings" element={<WithSync />} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const notice = () => screen.getByTestId('feature-previews-save-error').textContent ?? ''

beforeEach(() => {
  localStorage.clear()
  // Synced, so no one-time carry runs and every PATCH below is a click.
  localStorage.setItem(PREVIEW_FLAGS_SYNCED_KEY, '1')
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
  vi.restoreAllMocks()
})

describe('Feature previews section, refused toggles', () => {
  it.each([
    [/artifact deploy/i, 'Artifact Deploy'],
    [/webhooks/i, 'Webhooks'],
    [/^ultra mode/i, 'Ultra Mode'],
    [/chat on a crew/i, 'Chat on a crew'],
    [/composable layout/i, 'Composable layout'],
    [/folder conductor/i, 'Folder conductor'],
    [/agent harness per mate/i, 'Agent harness per mate'],
    [/session environments/i, 'Session Environments'],
  ])('names the %s switch when the owner-only write is refused', async (name, label) => {
    vi.spyOn(api, 'patchConfig').mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    renderSection()
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    await act(async () => { screen.getAllByRole('switch', { name })[0].click() })
    await waitFor(() => expect(notice()).toContain(`“${label}” was switched back`))
    expect(notice()).toMatch(/only the owner of this Kiro Crew/i)
    // The refetch the refused write ends with has landed, so nothing is still
    // in flight when the next test resets the sync state.
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalledTimes(2))
  })

  it('says the save failed for any other refusal', async () => {
    vi.spyOn(api, 'patchConfig').mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    renderSection()
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    await act(async () => { screen.getAllByRole('switch', { name: /^ultra mode/i })[0].click() })
    await waitFor(() => expect(notice()).toMatch(/couldn’t save “Ultra Mode”, so it was switched back/i))
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalledTimes(2))
  })

  it('says a switch is on in this browser only when its one-time carry is refused', async () => {
    localStorage.removeItem(PREVIEW_FLAGS_SYNCED_KEY)
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    vi.spyOn(api, 'patchConfig').mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    renderSection()
    await waitFor(() => expect(notice()).toMatch(/“Webhooks” is on in this browser only/i))
    expect(notice()).toMatch(/it stays on until then/i)
    expect(notice()).not.toMatch(/for everyone/i)
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalledTimes(2))
    // And it really is still on here, as the notice says.
    expect(localStorage.getItem(PREVIEW_WEBHOOKS)).toBe('1')
    expect(screen.getAllByRole('switch', { name: /webhooks/i })[0].getAttribute('aria-checked')).toBe('true')
  })

  it('names the owner, not a reload, when a one-time carry is refused as owner-only', async () => {
    localStorage.removeItem(PREVIEW_FLAGS_SYNCED_KEY)
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    vi.spyOn(api, 'patchConfig').mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    renderSection()
    await waitFor(() => expect(notice()).toMatch(/“Webhooks” was turned on in this browser before it could be saved here/i))
    expect(notice()).toMatch(/only the owner of this Kiro Crew can save it/i)
    expect(notice()).toMatch(/stays on in this browser only/i)
    expect(notice()).not.toMatch(/for everyone/i)
    expect(notice()).not.toMatch(/reload/i)
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalledTimes(2))
    expect(screen.getAllByRole('switch', { name: /webhooks/i })[0].getAttribute('aria-checked')).toBe('true')
  })
})

describe('Feature previews section, ingress links', () => {
  it.each([
    [PREVIEW_ARTIFACT_DEPLOY, /open artifact deploy/i, '/deploy'],
    [PREVIEW_WEBHOOKS, /open webhooks/i, '/webhooks'],
  ])('opens the page %s reveals', async (flag, link, path) => {
    localStorage.setItem(flag, '1')
    vi.mocked(api.kirocrewConfig).mockResolvedValue(
      { dashboard: { feature_previews: { artifact_deploy: true, webhooks: true } } } as never,
    )
    renderSection()
    await act(async () => { screen.getByRole('button', { name: link }).click() })
    expect(screen.getByTestId('where').textContent).toBe(path)
  })
})
