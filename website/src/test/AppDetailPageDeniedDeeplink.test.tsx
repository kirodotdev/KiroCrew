/**
 * AppDetailPage — the third-party execution gate must be actionable.
 *
 * Before this, both failure paths rendered the backend's raw English sentence
 * ("blocked by execution policy: … set agent.apps_allow_third_party=true …")
 * straight into a dashboard translated into 10 languages, naming a config key
 * with nothing to click. A user who never opens a terminal was simply stuck.
 *
 * What these tests pin:
 *  1. the affordance keys off the machine-readable `code`, NOT the prose — the
 *     prose is English, unlocalizable, and free to be reworded by the backend;
 *  2. it works for BOTH shapes, because the two paths fail differently: the
 *     registry install RESOLVES a payload carrying `code`, while `enableApp`
 *     REJECTS with an ApiError that keeps the payload as a JSON *string* on
 *     `.body`;
 *  3. an unrelated failure still shows its own message and offers no button —
 *     a stale flag must never mislabel the next error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useSearchParams } from 'react-router-dom'

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

const BLOCKED_COPY = /not allowed to run their own code yet/i
const BUTTON = /open security settings/i

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

/** Renders the search string of the /settings route it landed on.
 *
 *  Asserting only that "settings page" appeared would prove `/settings`
 *  MATCHED and nothing more — a button changed to a bare `/settings` would
 *  still pass while the user lands on the default tab. So surface the query
 *  and assert the tab itself.
 */
function SettingsProbe() {
  const [params] = useSearchParams()
  return <div>settings tab: {params.get('tab') || '(none)'}</div>
}

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={[{ pathname: '/apps/detail/launchdarkly' }]}>
      <Routes>
        <Route path="/apps/detail/:name" element={<AppDetailPage />} />
        <Route path="/apps" element={<div>apps list</div>} />
        <Route path="/settings" element={<SettingsProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('AppDetailPage — third-party execution denial', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    system.mockResolvedValue({})
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    getApp.mockResolvedValue(INSTALLED_APP)
  })

  it('enable denial (ApiError with JSON body) offers the security-settings button', async () => {
    // Mirrors the real client: ApiError keeps the payload as a raw JSON STRING
    // on .body, so reading err.code directly finds nothing.
    const err = Object.assign(new Error(DENIED_PROSE), {
      name: 'ApiError',
      status: 400,
      body: JSON.stringify({ ok: false, name: 'launchdarkly', error: DENIED_PROSE, code: 'app_execution_denied' }),
    })
    enableApp.mockRejectedValue(err)

    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))

    expect(await screen.findByText(BLOCKED_COPY)).toBeInTheDocument()
    // The raw config-key sentence must not be what the user reads.
    expect(screen.queryByText(/apps_allow_third_party/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: BUTTON })).toBeInTheDocument()
  })

  it('the button navigates to the Security settings tab', async () => {
    enableApp.mockRejectedValue(Object.assign(new Error(DENIED_PROSE), {
      name: 'ApiError', status: 400,
      body: JSON.stringify({ error: DENIED_PROSE, code: 'app_execution_denied' }),
    }))

    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))
    fireEvent.click(await screen.findByRole('button', { name: BUTTON }))

    // The TAB is the point — landing on /settings with the default tab would
    // leave the user hunting for the switch we just told them about.
    expect(await screen.findByText('settings tab: security')).toBeInTheDocument()
  })

  it('install denial (resolved payload carrying code) offers the same button', async () => {
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

    expect(await screen.findByText(BLOCKED_COPY)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: BUTTON })).toBeInTheDocument()
  })

  it('an unrelated failure keeps its own message and offers no button', async () => {
    enableApp.mockRejectedValue(Object.assign(new Error('disk on fire'), {
      name: 'ApiError', status: 500, body: JSON.stringify({ error: 'disk on fire' }),
    }))

    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))

    expect(await screen.findByText('disk on fire')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: BUTTON })).not.toBeInTheDocument()
    expect(screen.queryByText(BLOCKED_COPY)).not.toBeInTheDocument()
  })

  it('dismissing clears the denial so a later error is not mislabelled', async () => {
    enableApp.mockRejectedValueOnce(Object.assign(new Error(DENIED_PROSE), {
      name: 'ApiError', status: 400,
      body: JSON.stringify({ error: DENIED_PROSE, code: 'app_execution_denied' }),
    }))

    renderDetail()
    const enableBtn = await screen.findByRole('button', { name: /enable/i })
    fireEvent.click(enableBtn)
    await screen.findByRole('button', { name: BUTTON })

    fireEvent.click(screen.getByRole('button', { name: /dismiss error/i }))
    await waitFor(() => expect(screen.queryByRole('button', { name: BUTTON })).not.toBeInTheDocument())

    // A different failure now must not inherit the third-party copy.
    enableApp.mockRejectedValue(Object.assign(new Error('disk on fire'), {
      name: 'ApiError', status: 500, body: JSON.stringify({ error: 'disk on fire' }),
    }))
    fireEvent.click(screen.getByRole('button', { name: /enable/i }))

    expect(await screen.findByText('disk on fire')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: BUTTON })).not.toBeInTheDocument()
  })
})
