/**
 * Trust-consent gate for third-party app code execution.
 *
 * Covers the contract that matters for security UX: the gate opens on the
 * machine-readable `app_execution_denied` CODE (never on message text),
 * confirming grants trust for exactly one app and then retries the enable,
 * every other enable failure stays a plain error, and Cancel grants nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

// --- Mocks -----------------------------------------------------------------
const listApps = vi.fn()
const listRegistry = vi.fn()
const listRegistries = vi.fn()
const enableApp = vi.fn()
const untrustApp = vi.fn()
const trustApp = vi.fn()
const getApp = vi.fn()
const system = vi.fn()
const installFromRegistryStream = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    listRegistries: (...a: unknown[]) => listRegistries(...a),
    updateRegistries: vi.fn(),
    refreshRegistries: vi.fn(),
    enableApp: (...a: unknown[]) => enableApp(...a),
    trustApp: (...a: unknown[]) => trustApp(...a),
    untrustApp: (...a: unknown[]) => untrustApp(...a),
    getApp: (...a: unknown[]) => getApp(...a),
    system: (...a: unknown[]) => system(...a),
    installFromRegistryStream: (...a: unknown[]) => installFromRegistryStream(...a),
    disableApp: vi.fn(),
    updateApp: vi.fn(),
    uninstallApp: vi.fn(),
    uninstallPreview: vi.fn().mockResolvedValue({ dependencies: { removable: [], shared: [], userInstalled: [] } }),
    installApp: vi.fn(),
    openApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

// Render catalog KEYS, not English. The trust-modal strings are authored in the
// locale catalogs; asserting on their English would make this suite a copy of
// the copywriting and break on any reword. Interpolated values are appended so
// the {{app}} threading is still observable.
vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length ? `${key} ${Object.values(vars).join(' ')}` : key,
}))

vi.mock('../components/AppIcon', () => ({
  default: () => <div data-testid="app-icon" />,
}))

// SegmentedControl measures its container (0px in jsdom) and collapses to a
// dropdown, hiding tab labels — stub it with plain buttons.
vi.mock('../components/SegmentedControl', () => ({
  default: ({ segments, onChange }: {
    segments: { key: string; label: string }[]
    onChange: (key: string) => void
  }) => (
    <div>
      {segments.map(s => (
        <button key={s.key} type="button" onClick={() => onChange(s.key)}>{s.label}</button>
      ))}
    </div>
  ),
}))

import AppsPage from '../pages/AppsPage'
import AppDetailPage from '../pages/AppDetailPage'
import { isTrustDeniedError, APP_EXECUTION_DENIED, safeHref } from '../components/appstore/TrustAppModal'

/** An ApiError-shaped rejection: message plus the raw structured body. */
function apiError(status: number, body: object, message = 'boom') {
  return Object.assign(new Error(message), { status, body: JSON.stringify(body) })
}

const TRUST_DENIED = () => apiError(403, {
  error: 'App third-party is not trusted to run its own code.',
  code: APP_EXECUTION_DENIED,
})

/**
 * How a REFUSED registry install arrives: the SSE stream resolves its `done`
 * payload, so the code travels on the result rather than on a rejection.
 */
const INSTALL_DENIED = () => ({
  ok: false,
  name: 'launchdarkly',
  error: 'blocked by execution policy: App launchdarkly is not trusted to run its own code.',
  code: APP_EXECUTION_DENIED,
  log: '',
})

const THIRD_PARTY = {
  name: 'launchdarkly',
  displayName: 'LaunchDarkly',
  description: 'Feature flags in your agentic workspace.',
  version: '1.0.0',
  author: 'launchdarkly',
  repo: 'https://github.com/launchdarkly-labs/launchdarkly-kiro-crew-app',
  tags: ['feature-flags'],
  featured: 1,
  installed: true,
  enabled: false,
  origin: 'registry',
  updateAvailable: false,
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<AppsPage />} />
          <Route path="/apps/detail/:name" element={<div data-testid="detail-route" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/**
 * Render the DETAIL page the way the App Store's Get button reaches it.
 *
 * Get on AppsPage (and on FeaturedSpotlight, which takes `onGet` as a prop)
 * navigates to `/apps/detail/:name` with `autoAction: 'install'` in ROUTER STATE
 * — never a query param — and the detail page runs the install from there. So the
 * install-refusal consent flow is exercised here, at the surface that owns the
 * install call.
 */
function renderDetailFromGet(name = THIRD_PARTY.name) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[{ pathname: `/apps/detail/${name}`, state: { autoAction: 'install' } }]}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
          <Route path="/apps" element={<div data-testid="apps-route" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/**
 * Click Enable on the third-party app.
 *
 * The Library tab is the surface that offers it: FeaturedSpotlight/AppListRow
 * only render Enable for a hidden BUILT-IN, so an installed-but-disabled
 * third-party app is enabled from its installed card (or the detail page).
 */
async function clickEnable() {
  fireEvent.click(await screen.findByRole('button', { name: /appsPage\.library/ }))
  const btn = await screen.findByRole('button', { name: /installedAppCard\.enable$/ })
  fireEvent.click(btn)
  return btn
}

const K = 'components.appstore.trustAppModal'
const modalTitle = () => screen.queryByText(`${K}.title ${THIRD_PARTY.displayName}`)
const confirmBtn = () => screen.getByRole('button', { name: new RegExp(`${K}\\.(confirm|working)`) })
const cancelBtn = () => screen.getByRole('button', { name: new RegExp(`${K}\\.cancel`) })

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  listApps.mockResolvedValue([
    {
      name: THIRD_PARTY.name, displayName: THIRD_PARTY.displayName, version: '1.0.0',
      enabled: false, installedAt: '2026-08-03T00:00:00Z', origin: 'registry',
      manifest: {
        name: THIRD_PARTY.name, version: '1.0.0', displayName: THIRD_PARTY.displayName,
        description: THIRD_PARTY.description, author: THIRD_PARTY.author, repo: THIRD_PARTY.repo,
      },
    },
  ])
  listRegistry.mockResolvedValue({ apps: [THIRD_PARTY], serverPlatform: { os: 'darwin', arch: 'arm64' } })
  listRegistries.mockResolvedValue({ registries: [] })
  trustApp.mockResolvedValue({ apps: [THIRD_PARTY.name], ineffective: [], allowAll: false })
  untrustApp.mockResolvedValue({ apps: [], ineffective: [], allowAll: false })
  // Detail-page load: not installed yet, so the registry entry is the source of
  // truth and the page offers Get rather than Enable/Disable.
  getApp.mockRejectedValue(new Error('not installed'))
  system.mockResolvedValue({ hostname: 'localhost' })
})

describe('isTrustDeniedError', () => {
  it('matches only the app_execution_denied code, ignoring the message text', () => {
    expect(isTrustDeniedError(TRUST_DENIED())).toBe(true)
    // Same English wording, different code → NOT the trust gate.
    expect(isTrustDeniedError(apiError(403, {
      error: 'App third-party is not trusted to run its own code.',
      code: 'some_other_code',
    }))).toBe(false)
    expect(isTrustDeniedError(apiError(500, { error: 'kaboom' }))).toBe(false)
    expect(isTrustDeniedError(new Error('app_execution_denied'))).toBe(false)
    expect(isTrustDeniedError(undefined)).toBe(false)
  })

  it('also matches a RESOLVED install result, which carries the code on the payload', () => {
    // The SSE install stream reports a refusal by RESOLVING `done` with this
    // shape, so a check that only understood rejections would never open the
    // consent modal on the install path.
    expect(isTrustDeniedError(INSTALL_DENIED())).toBe(true)
    expect(isTrustDeniedError({ ok: false, error: 'clone failed' })).toBe(false)
  })
})

describe('AppsPage trust gate', () => {
  it('opens the consent modal when enable is refused with app_execution_denied', async () => {
    enableApp.mockRejectedValue(TRUST_DENIED())
    renderPage()
    await clickEnable()

    await waitFor(() => expect(modalTitle()).toBeTruthy())
    // Scope disclosure, the three capabilities, and the provenance line.
    expect(screen.getByText(`${K}.scope`)).toBeTruthy()
    expect(screen.getByText(`${K}.capability_python`)).toBeTruthy()
    expect(screen.getByText(`${K}.capability_backend`)).toBeTruthy()
    expect(screen.getByText(`${K}.capability_shell`)).toBeTruthy()
    expect(screen.getByText(`${K}.source`)).toBeTruthy()
    expect(screen.getByText(THIRD_PARTY.repo)).toBeTruthy()
    // The raw backend string never reaches the user.
    expect(screen.queryByText(/is not trusted to run its own code/)).toBeNull()
  })

  it('grants trust for that one app and retries the enable on confirm', async () => {
    enableApp.mockRejectedValueOnce(TRUST_DENIED()).mockResolvedValue({ ok: true })
    renderPage()
    await clickEnable()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(trustApp).toHaveBeenCalledWith(THIRD_PARTY.name))
    await waitFor(() => expect(enableApp).toHaveBeenCalledTimes(2))
    expect(trustApp).toHaveBeenCalledTimes(1)
    // Grant landed and the retry succeeded → the modal closes.
    await waitFor(() => expect(modalTitle()).toBeNull())
  })

  it('keeps the modal open and reports inline when the retried enable fails', async () => {
    enableApp.mockRejectedValue(TRUST_DENIED())
    renderPage()
    await clickEnable()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(screen.getByRole('alert').textContent)
      .toBe(`${K}.failed LaunchDarkly`))
    expect(modalTitle()).toBeTruthy()
  })

  it('does NOT open the modal for a non-trust enable failure', async () => {
    enableApp.mockRejectedValue(apiError(500, { error: 'gateway exploded' }, 'gateway exploded'))
    renderPage()
    await clickEnable()

    await waitFor(() => expect(screen.getByText(/gateway exploded/)).toBeTruthy())
    expect(modalTitle()).toBeNull()
    expect(trustApp).not.toHaveBeenCalled()
  })

  it('grants nothing when the user cancels', async () => {
    enableApp.mockRejectedValue(TRUST_DENIED())
    renderPage()
    await clickEnable()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(cancelBtn())

    await waitFor(() => expect(modalTitle()).toBeNull())
    expect(trustApp).not.toHaveBeenCalled()
    expect(enableApp).toHaveBeenCalledTimes(1)
  })
})

/**
 * The INSTALL side of the same gate.
 *
 * The registry install path checks the execution gate BEFORE cloning, so a Get on
 * an untrusted third-party app is refused before anything reaches disk. That
 * refusal must open the same consent modal — and confirming must retry the
 * INSTALL, not the enable: nothing is installed yet, so an enable retry would
 * fail on a missing app and strand the user.
 */
describe('registry install trust gate', () => {
  /** Same app, not installed yet — the state a Get starts from. */
  const NOT_INSTALLED = { ...THIRD_PARTY, installed: false, enabled: false }

  beforeEach(() => {
    listRegistry.mockResolvedValue({ apps: [NOT_INSTALLED], serverPlatform: { os: 'darwin', arch: 'arm64' } })
  })

  it('opens the consent modal when the install is refused with app_execution_denied', async () => {
    installFromRegistryStream.mockResolvedValue(INSTALL_DENIED())
    renderDetailFromGet()

    await waitFor(() => expect(modalTitle()).toBeTruthy())
    expect(screen.getByText(`${K}.capability_python`)).toBeTruthy()
    // The raw backend sentence never reaches the user.
    expect(screen.queryByText(/blocked by execution policy/)).toBeNull()
  })

  it('grants trust then retries the INSTALL — never the enable', async () => {
    installFromRegistryStream
      .mockResolvedValueOnce(INSTALL_DENIED())
      .mockResolvedValue({ ok: true, name: THIRD_PARTY.name })
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())
    expect(installFromRegistryStream).toHaveBeenCalledTimes(1)

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(trustApp).toHaveBeenCalledWith(THIRD_PARTY.name))
    // The retry is the install, re-run once for the same app.
    await waitFor(() => expect(installFromRegistryStream).toHaveBeenCalledTimes(2))
    expect(installFromRegistryStream.mock.calls[1][0]).toBe(THIRD_PARTY.name)
    expect(enableApp).not.toHaveBeenCalled()
    // Grant landed and the retried install succeeded → the modal closes.
    await waitFor(() => expect(modalTitle()).toBeNull())
  })

  it('keeps the modal open and reports inline when the retried install is refused again', async () => {
    // A grant that did not take effect must not look like success.
    installFromRegistryStream.mockResolvedValue(INSTALL_DENIED())
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(screen.getByRole('alert').textContent)
      .toBe(`${K}.failed LaunchDarkly`))
    expect(modalTitle()).toBeTruthy()
  })

  it('rolls the grant back when the failed install left no app to own it', async () => {
    // REGRESSION: trust is granted BEFORE the retry, so a failed install left a
    // grant over a name no app occupies. Grants are keyed on the name alone, so
    // whatever is installed under it next would run its own code with no consent
    // prompt — the very state the uninstall path refuses to create. `getApp`
    // 404s here, which is how "not installed" really arrives (j() rejects).
    installFromRegistryStream.mockResolvedValue(INSTALL_DENIED())
    getApp.mockRejectedValue(apiError(404, { error: 'app not installed' }))
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(untrustApp).toHaveBeenCalledWith(THIRD_PARTY.name))
    // Nothing was left behind, so the copy must say that rather than sending the
    // user to Settings to remove a grant that is already gone.
    await waitFor(() => expect(screen.getByRole('alert').textContent)
      .toBe(`${K}.failed_generic LaunchDarkly`))
    expect(modalTitle()).toBeTruthy()
  })

  it('KEEPS the grant when absence cannot be proven — only a 404 rolls back', async () => {
    // The other half, and the anti-guess rule. Only a 404 proves the name is
    // unoccupied; a network error or a 500 proves nothing, and revoking on that
    // would switch off an app that exists and works. The suite default rejects
    // `getApp` with a plain Error (no status), so this is that branch: the grant
    // stands and the copy points the user at Settings to review it.
    installFromRegistryStream.mockResolvedValue(INSTALL_DENIED())
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(screen.getByRole('alert').textContent)
      .toBe(`${K}.failed LaunchDarkly`))
    expect(untrustApp).not.toHaveBeenCalled()
  })

  it('refreshes the trusted-apps views after a grant, so no surface serves a pre-grant snapshot', async () => {
    // The hook mutates trust through `api.trustApp` directly rather than a
    // `useMutation`, so nothing invalidated the queries that RENDER the result:
    // the Security panel's trusted-apps list and the App Store's rows kept
    // serving a cached pre-grant snapshot — a settings surface showing a stale
    // answer about who is allowed to run code.
    const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries')
    installFromRegistryStream
      .mockResolvedValueOnce(INSTALL_DENIED())
      .mockResolvedValue({ ok: true, name: THIRD_PARTY.name })
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())
    await waitFor(() => expect(modalTitle()).toBeNull())

    const keys = invalidate.mock.calls.map(c => JSON.stringify(
      (c[0] as { queryKey?: unknown } | undefined)?.queryKey,
    ))
    expect(keys).toContain('["trusted-apps"]')
    expect(keys).toContain('["apps"]')
    invalidate.mockRestore()
  })

  it('rolls the grant back when the retried install fails for an ORDINARY reason', async () => {
    // REGRESSION: `runInstall()` reported `'done'` for a plain `{ok:false}` install
    // failure, so the retry resolved, so `useTrustGate` never rejected, so the
    // rollback never fired — leaving a grant over a name no app occupies. Only a
    // SECOND trust refusal used to reject. Every unsuccessful install must now.
    installFromRegistryStream
      .mockResolvedValueOnce(INSTALL_DENIED())
      .mockResolvedValue({ ok: false, error: 'git clone exploded' })
    getApp.mockRejectedValue(apiError(404, { error: 'app not installed' }))
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(untrustApp).toHaveBeenCalledWith(THIRD_PARTY.name))
    // Nothing was left behind, so the copy says so rather than sending the user to
    // Settings after a grant that is already gone.
    await waitFor(() => expect(screen.getByRole('alert').textContent)
      .toBe(`${K}.failed_generic LaunchDarkly`))
  })

  it('rolls the grant back when the retried install is ABORTED', async () => {
    // REGRESSION: the AbortError path returned `'done'`, so an install aborted by
    // navigating away resolved as success — the retry never rejected, the rollback
    // never fired, and the fresh grant stayed over a name no app occupies. Third
    // door to the same orphan, after an ordinary `{ok:false}` and a second refusal.
    const aborted = Object.assign(new Error('aborted'), { name: 'AbortError' })
    installFromRegistryStream
      .mockResolvedValueOnce(INSTALL_DENIED())
      .mockRejectedValue(aborted)
    getApp.mockRejectedValue(apiError(404, { error: 'app not installed' }))
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(untrustApp).toHaveBeenCalledWith(THIRD_PARTY.name))
    // The KEEP direction (abort raced a COMPLETED install, so the app exists and
    // the grant is rightly kept) is covered by the `absence cannot be proven` test
    // above: both go through the same `isNotFound` probe, and only a 404 rolls back.
  })

  it('does NOT open the modal for an ordinary install failure', async () => {
    installFromRegistryStream.mockResolvedValue({ ok: false, error: 'git clone exploded' })
    renderDetailFromGet()

    await waitFor(() => expect(screen.getByText(/git clone exploded/)).toBeTruthy())
    expect(modalTitle()).toBeNull()
    expect(trustApp).not.toHaveBeenCalled()
  })

  it('grants nothing when the user cancels the install consent', async () => {
    installFromRegistryStream.mockResolvedValue(INSTALL_DENIED())
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(cancelBtn())

    await waitFor(() => expect(modalTitle()).toBeNull())
    expect(trustApp).not.toHaveBeenCalled()
    expect(installFromRegistryStream).toHaveBeenCalledTimes(1)
  })
})

describe('safeHref — the provenance link is not a script sink', () => {
  // REGRESSION: `app.repo` is registry-index content. Rendering it straight into
  // `href` made `javascript:...` a one-click script-execution vector in the
  // dashboard's own origin — on the very dialog whose job is to gate code
  // execution. The link was added to satisfy a usability finding and opened this.
  it('accepts http(s) and refuses every script-capable scheme', () => {
    expect(safeHref('https://github.com/owner/repo')).toBe('https://github.com/owner/repo')
    expect(safeHref('http://example.com/x')).toBe('http://example.com/x')
    for (const bad of [
      'javascript:alert(1)',
      'JaVaScRiPt:alert(1)',
      '\tjavascript:alert(1)',
      ' javascript:alert(1)',
      'data:text/html,<script>alert(1)</script>',
      'blob:https://example.com/uuid',
      'file:///etc/passwd',
      'vbscript:msgbox(1)',
      'not a url at all',
      '',
    ]) {
      expect(safeHref(bad)).toBeNull()
    }
  })
})
