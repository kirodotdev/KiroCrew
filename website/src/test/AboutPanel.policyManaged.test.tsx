// When the gateway's security policy provider governs updates on a bundled
// install AND can actually apply on this platform, the desktop update section
// shows a "managed by policy" notice instead of the regular update card and
// auto-download switch.
//
// Three conditions must all be true:
//   update_bundled_by_app: true  (gateway is the one the app bundles)
//   update_managed_by: 'command' (a policy provider owns updates)
//   update_can_apply: true       (the provider shell exists — not Windows)
//
// On Windows the policy shell is absent (can_apply: false), so the Electron
// updater remains the working update path and the notice does not appear.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'
import { i18nT } from '../i18n/t'
import type { StatusData } from '../types'

afterEach(cleanup)

const BASE_INFO = {
  version: '0.1.0',
  channel: 'stable',
  stampedChannel: 'stable',
  channelSwitchable: true,
  channelPreference: '',
  platform: 'darwin-arm64',
  packaged: true,
}

function mountWithStatus(status: Partial<StatusData>) {
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
    onState: () => () => {},
    check: vi.fn().mockResolvedValue({ ok: true }),
    download: vi.fn().mockResolvedValue({ ok: true }),
    install: vi.fn().mockResolvedValue({ ok: true }),
    getInfo: vi.fn().mockResolvedValue(BASE_INFO),
  }
  store.dispatch(sseStatus(status as StatusData))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('policy-governed bundled install', () => {
  it('shows the "managed by policy" notice when provider can apply on this platform', async () => {
    mountWithStatus({ update_bundled_by_app: true, update_managed_by: 'command', update_can_apply: true } as Partial<StatusData>)
    const note = await screen.findByTestId('gateway-policy-managed-note')
    expect(note).toBeInTheDocument()
    expect(note.textContent).toContain(i18nT('pages.settings.aboutPanel.updates_managed_by_policy_desktop'))
  })

  it('hides the auto-download switch when policy governs and provider can apply', async () => {
    mountWithStatus({ update_bundled_by_app: true, update_managed_by: 'command', update_can_apply: true } as Partial<StatusData>)
    // Wait for the panel to render the version chip so the assertion is against
    // a fully settled tree, not the initial loading state.
    await screen.findByTestId('gateway-policy-managed-note')
    const switches = screen.queryAllByRole('switch', { name: /install app updates automatically/i })
    expect(switches).toHaveLength(0)
  })

  it('does not show the notice when the provider cannot apply on this platform (Windows)', async () => {
    // On Windows the policy shell is absent: update_can_apply is false.
    // The Electron updater remains the working update path.
    mountWithStatus({ update_bundled_by_app: true, update_managed_by: 'command', update_can_apply: false } as Partial<StatusData>)
    await waitFor(() => {
      expect(screen.queryByTestId('gateway-policy-managed-note')).not.toBeInTheDocument()
    })
  })

  it('does not show the notice when bundled but no policy provider', async () => {
    mountWithStatus({ update_bundled_by_app: true, update_managed_by: 'electron', update_can_apply: false } as Partial<StatusData>)
    await waitFor(() => {
      expect(screen.queryByTestId('gateway-policy-managed-note')).not.toBeInTheDocument()
    })
  })

  it('does not show the notice when policy provider but not bundled', async () => {
    mountWithStatus({ update_bundled_by_app: false, update_managed_by: 'command', update_can_apply: true } as Partial<StatusData>)
    await waitFor(() => {
      expect(screen.queryByTestId('gateway-policy-managed-note')).not.toBeInTheDocument()
    })
  })
})
