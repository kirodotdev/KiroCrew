/**
 * The Crewmate 2.0 switch sits inside the Crew Members preview card and only
 * while that preview is on; it writes the server config `dashboard.crewmate_v2`
 * and says a restart applies it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../../api/client'
import { PREVIEW_CREW } from '../../utils/previewFlags'
import { FeaturePreviewsSection } from './FeaturePreviewsSection'
import { CREWMATE_V2_CONFIG_KEY } from './CrewmateV2Toggle'
import { i18nT } from '../../i18n/t'

const L = (k: string) => i18nT(`pages.developer.featurePreviewsTab.${k}`)

describe('Crewmate 2.0 toggle', () => {
  let cfg: { dashboard?: { crewmate_v2?: boolean } }
  beforeEach(() => {
    localStorage.clear()
    cfg = { dashboard: { crewmate_v2: false } }
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: false } as never)
    vi.spyOn(api, 'kirocrewConfig').mockImplementation(async () => cfg as never)
    vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue({
      enabled: false, endpoint: '', configured_endpoint: '', permits: false,
    })
  })
  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  const renderTab = () =>
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter><FeaturePreviewsSection /></MemoryRouter>
      </QueryClientProvider>,
    )

  it('is hidden while the Crewmates preview is off', () => {
    renderTab()
    expect(screen.queryByTestId('crewmate-v2-toggle')).toBeNull()
    expect(screen.queryByRole('switch', { name: L('crewmate_v2') })).toBeNull()
  })

  it('shows, off, nested in the Crewmates card once that preview is on', async () => {
    localStorage.setItem(PREVIEW_CREW, '1')
    renderTab()
    const nested = await screen.findByTestId('crewmate-v2-toggle')
    const crewSwitch = screen.getByRole('switch', { name: /^crewmates$/i })
    // The same card: the Crewmates switch and the nested one share a parent card.
    expect(crewSwitch.closest('[data-testid="crewmate-v2-toggle"]')).toBeNull()
    expect(nested.parentElement?.contains(crewSwitch)).toBe(true)
    const v2 = within(nested).getByRole('switch', { name: L('crewmate_v2') })
    await waitFor(() => expect(v2.getAttribute('aria-disabled')).not.toBe('true'))
    expect(v2.getAttribute('aria-checked')).toBe('false')
  })

  it('writes the config key and says a restart applies it', async () => {
    localStorage.setItem(PREVIEW_CREW, '1')
    const patch = vi.spyOn(api, 'patchConfig').mockImplementation(async (_p, v) => {
      cfg = { dashboard: { crewmate_v2: v as boolean } }
      return { ok: true } as never
    })
    renderTab()
    const nested = await screen.findByTestId('crewmate-v2-toggle')
    const v2 = within(nested).getByRole('switch', { name: L('crewmate_v2') })
    await waitFor(() => expect(v2.getAttribute('aria-disabled')).not.toBe('true'))
    await act(async () => { v2.click() })
    expect(patch).toHaveBeenCalledWith(CREWMATE_V2_CONFIG_KEY, true)
    expect((await screen.findByTestId('crewmate-v2-restart')).textContent).toBe(L('crewmate_v2_restart'))
    await waitFor(() => expect(v2.getAttribute('aria-checked')).toBe('true'))
  })
})
