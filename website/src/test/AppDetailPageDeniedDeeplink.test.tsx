/**
 * AppDetailPage — the third-party execution gate must be actionable.
 *
 * Before this, both failure paths rendered the backend's raw English sentence
 * ("blocked by execution policy: … set agent.apps_allow_third_party=true …")
 * straight into a dashboard translated into 10 languages, naming a config key
 * with nothing to click. A user who never opens a terminal was simply stuck.
 *
 * The first fix for that deep-linked to Settings → Security so the user could
 * flip `apps_allow_third_party`. Per-app trust grants SUPERSEDE that: the same
 * denial now opens the consent modal and authorises THIS app, so wanting one
 * app no longer authorises every future one. The deeplink is gone because it
 * became unreachable — the trust check consumes the identical error code first.
 *
 * What these tests pin:
 *  1. the affordance keys off the machine-readable `code`, NOT the prose — the
 *     prose is English, unlocalizable, and free to be reworded by the backend;
 *  2. it works for BOTH shapes, because the two paths fail differently: the
 *     registry install RESOLVES a payload carrying `code`, while `enableApp`
 *     REJECTS with an ApiError that keeps the payload as a JSON *string* on
 *     `.body`;
 *  3. an unrelated failure still shows its own message and opens no modal —
 *     a denial must never be inferred from an arbitrary error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()
const installFromRegistryStream = vi.fn()
const enableApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
    installFromRegistryStream: (...a: unknown[]) => installFromRegistryStream(...a),
    enableApp: (...a: unknown[]) => enableApp(...a),
    disableApp: vi.fn(),
    updateApp: vi.fn(),
    uninstallApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'

const DENIED_PROSE =
  'blocked by execution policy: third-party app execution is disabled; explicitly set '
  + 'agent.apps_allow_third_party=true to allow Python, backend, and manifest shell code'

/** The consent modal's heading ("Trust {{app}} to run its own code?") — the
 *  affordance that replaced the deeplink. */
const TRUST_MODAL = /to run its own code\?/i

/** An installed-but-disabled app, so the Enable action is on screen. */
const INSTALLED_APP = {
  name: 'launchdarkly',
  displayName: 'LaunchDarkly',
  description: 'Flag control tower.',
  version: '0.2.0',
  installedVersion: '0.2.0',
  author: 'kirocrew',
  installed: true,
  enabled: false,
  manifest: { name: 'launchdarkly', version: '0.2.0', displayName: 'LaunchDarkly' },
}

function renderDetail() {
  // `useTrustGate` invalidates the ['trusted-apps'] / ['apps'] queries after a
  // grant so the Security panel and the App Store cannot serve a pre-grant
  // snapshot, which means the hook needs a QueryClient in scope. The app root
  // always provides one; the harness has to as well. `retry: false` keeps a
  // rejected query from re-firing and slowing the failure assertions.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[{ pathname: '/apps/detail/launchdarkly' }]}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
          <Route path="/apps" element={<div>apps list</div>} />
          <Route path="/settings" element={<div>settings page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AppDetailPage — third-party execution denial', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    system.mockResolvedValue({})
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    getApp.mockResolvedValue(INSTALLED_APP)
  })

  it('enable denial (ApiError with JSON body) opens the trust consent modal', async () => {
    // Mirrors the real client: ApiError keeps the payload as a raw JSON STRING
    // on .body, so reading err.code directly finds nothing.
    const err = Object.assign(new Error(DENIED_PROSE), {
      name: 'ApiError',
      status: 403,
      body: JSON.stringify({ ok: false, name: 'launchdarkly', error: DENIED_PROSE, code: 'app_execution_denied' }),
    })
    enableApp.mockRejectedValue(err)

    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))

    expect(await screen.findByText(TRUST_MODAL)).toBeInTheDocument()
    // The raw config-key sentence must not be what the user reads.
    expect(screen.queryByText(/apps_allow_third_party/)).not.toBeInTheDocument()
  })

  it('the superseded security-settings deeplink is gone', async () => {
    enableApp.mockRejectedValue(Object.assign(new Error(DENIED_PROSE), {
      name: 'ApiError', status: 403,
      body: JSON.stringify({ error: DENIED_PROSE, code: 'app_execution_denied' }),
    }))

    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))
    await screen.findByText(TRUST_MODAL)

    // Granting THIS app is the resolution; sending the user to flip the blanket
    // switch would authorise every third-party app at once.
    expect(screen.queryByRole('button', { name: /open security settings/i })).not.toBeInTheDocument()
  })

  it('install denial (resolved payload carrying code) opens the same modal', async () => {
    getApp.mockResolvedValue(null)
    listRegistry.mockResolvedValue({
      apps: [{ ...INSTALLED_APP, installed: false, enabled: false }],
      serverPlatform: { os: 'darwin', arch: 'arm64' },
    })
    installFromRegistryStream.mockResolvedValue({
      ok: false, error: DENIED_PROSE, code: 'app_execution_denied',
    })

    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /install/i }))

    expect(await screen.findByText(TRUST_MODAL)).toBeInTheDocument()
  })

  it('an unrelated failure keeps its own message and opens no modal', async () => {
    enableApp.mockRejectedValue(Object.assign(new Error('disk on fire'), {
      name: 'ApiError', status: 500, body: JSON.stringify({ error: 'disk on fire' }),
    }))

    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))

    expect(await screen.findByText('disk on fire')).toBeInTheDocument()
    expect(screen.queryByText(TRUST_MODAL)).not.toBeInTheDocument()
  })
})
