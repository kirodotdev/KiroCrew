/**
 * Settings > People exists only while the gateway reports the Shared Crew
 * preview (`KIROCREW_SHARED_CREW_PREVIEW=1`). The gate is the capabilities
 * answer alone: no browser setting turns it on.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ALL_UNAVAILABLE } from '../api/sharedCrewUi'

const caps = vi.hoisted(() => vi.fn())
vi.mock('../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, sharedCrewCapabilities: caps } }
})
vi.mock('../pages/settings/OverviewPanel', () => ({ OverviewPanel: () => <div data-testid="overview-panel" /> }))
vi.mock('../pages/settings/PeoplePanel', () => ({ PeoplePanel: () => <div data-testid="people-panel" /> }))
vi.mock('../pages/settings/SettingsSearch', () => ({ default: () => null }))
vi.mock('../hooks/useSettingHighlight', async importOriginal => ({
  ...(await importOriginal<typeof import('../hooks/useSettingHighlight')>()),
  useSettingHighlight: () => {},
}))
vi.mock('../store', () => ({ useAppSelector: () => '1.0.0' }))

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

import SettingsPage from '../pages/SettingsPage'

function mount(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes><Route path="/settings/*" element={<SettingsPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const peopleRow = () => screen.queryAllByText('People').filter(el => el.closest('a, button, [role="tab"], [role="link"]'))

beforeEach(() => { localStorage.clear(); caps.mockReset() })
afterEach(() => { cleanup() })

describe('Settings > People behind the Shared Crew preview', () => {
  it.each([
    ['preview off', { ...ALL_UNAVAILABLE, preview: false }],
    ['an older gateway without the field', { ...ALL_UNAVAILABLE }],
  ])('%s: no People tab and no People panel, even at its own URL', async (_name, answer) => {
    caps.mockResolvedValue(answer)
    localStorage.setItem('kirocrew-shared-crew-preview', '1')
    mount('/settings/people')
    await waitFor(() => expect(caps).toHaveBeenCalled())
    await waitFor(() => expect(screen.getAllByText('Overview').length).toBeGreaterThan(0))
    expect(peopleRow()).toHaveLength(0)
    expect(screen.queryByTestId('people-panel')).toBeNull()
  })

  it('a failed capabilities read keeps People hidden', async () => {
    caps.mockRejectedValue(new Error('capabilities read failed'))
    mount('/settings/people')
    await waitFor(() => expect(caps).toHaveBeenCalled())
    await waitFor(() => expect(screen.getAllByText('Overview').length).toBeGreaterThan(0))
    expect(peopleRow()).toHaveLength(0)
    expect(screen.queryByTestId('people-panel')).toBeNull()
  })

  it('preview on: the People tab and panel show as today', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, preview: true })
    mount('/settings/people')
    expect(await screen.findByTestId('people-panel')).toBeInTheDocument()
    expect(peopleRow().length).toBeGreaterThan(0)
  })
})
