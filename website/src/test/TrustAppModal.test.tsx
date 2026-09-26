/**
 * Trust-consent gate for third-party app code execution.
 *
 * Covers the contract that matters for security UX: the gate opens on the
 * machine-readable `app_execution_denied` CODE (never on message text),
 * confirming grants trust for exactly one app and then retries the enable,
 * every other enable failure stays a plain error, and Cancel grants nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
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
    appContributors: vi.fn(() => Promise.resolve({ contributors: [] })),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

// happy-dom cannot drive real Radix menus — swap in the repo's stateful mock
// so the launchpad tile's overflow menu (where Enable now lives) opens.
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

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

import LibraryPage from '../pages/apps/LibraryPage'
import AppDetailPage from '../pages/AppDetailPage'
import {
  isTrustDeniedError,
  isSessionApprovalConsentRequiredError,
  retryFailureDetail,
  retryFailureCode,
  DESKTOP_BUILD_STEP_UNSUPPORTED,
  APP_EXECUTION_DENIED,
  SESSION_APPROVAL_CONSENT_REQUIRED,
  credentialFreeRepository,
  safeHref,
} from '../components/appstore/TrustAppModal'
import { __resetErrorJournalForTests, findReport } from '../utils/errorReport'

/** An ApiError-shaped rejection: message plus the raw structured body. */
function apiError(status: number, body: object, message = 'boom') {
  return Object.assign(new Error(message), { status, body: JSON.stringify(body) })
}

const TRUST_DENIED = () => apiError(403, {
  error: 'App third-party is not trusted to run its own code.',
  code: APP_EXECUTION_DENIED,
})

const SESSION_APPROVAL_REQUIRED = () => apiError(400, {
  error: 'session approval consent must be confirmed from a disclosure surface',
  code: SESSION_APPROVAL_CONSENT_REQUIRED,
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
  // `repo` is the legacy/display alias. The server-resolved clone target is
  // deliberately different so the modal cannot accidentally authorize this.
  repo: 'https://github.com/launchdarkly-labs/catalog-alias',
  trustRepository: 'https://git.example.test/launchdarkly/kiro-crew-app',
  tags: ['feature-flags'],
  featured: 1,
  installed: true,
  enabled: false,
  origin: 'registry',
  updateAvailable: false,
  manifest: { permissions: { sessionApproval: true } },
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps/library']}>
        <Routes>
          <Route path="/apps/library" element={<LibraryPage />} />
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
 * The Library page (/apps/library) is the surface that offers it:
 * FeaturedSpotlight/AppListRow only render Enable for a hidden BUILT-IN, so an
 * installed-but-disabled third-party app is enabled from its launchpad tile's
 * overflow menu (the tile caps its action row at two peers — Open plus the
 * menu — so Enable lives behind the MoreHorizontal trigger).
 */
async function clickEnable() {
  const trigger = await screen.findByRole('button', {
    name: `pages.libraryPage.tile_more_actions ${THIRD_PARTY.displayName}`,
  })
  fireEvent.click(trigger)
  const btn = await screen.findByRole('menuitem', { name: /installedAppCard\.enable$/ })
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
      trustRepository: THIRD_PARTY.trustRepository,
      manifest: {
        name: THIRD_PARTY.name, version: '1.0.0', displayName: THIRD_PARTY.displayName,
        description: THIRD_PARTY.description, author: THIRD_PARTY.author, repo: THIRD_PARTY.repo,
        permissions: { sessionApproval: true },
      },
    },
  ])
  listRegistry.mockResolvedValue({ apps: [THIRD_PARTY], serverPlatform: { os: 'darwin', arch: 'arm64' } })
  listRegistries.mockResolvedValue({ registries: [] })
  trustApp.mockResolvedValue({ apps: [THIRD_PARTY.name], ineffective: [], allowAll: false })
  untrustApp.mockResolvedValue({ apps: [], ineffective: [], allowAll: false })
  // Detail-page load: not installed yet, so the registry entry is the source of
  // truth and the page offers Get rather than Enable/Disable.
  getApp.mockRejectedValue(apiError(404, { error: 'not installed' }))
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

describe('isSessionApprovalConsentRequiredError', () => {
  it('matches only the consent-required code', () => {
    expect(isSessionApprovalConsentRequiredError(SESSION_APPROVAL_REQUIRED())).toBe(true)
    expect(isSessionApprovalConsentRequiredError(TRUST_DENIED())).toBe(false)
    expect(isSessionApprovalConsentRequiredError(new Error(SESSION_APPROVAL_CONSENT_REQUIRED))).toBe(false)
  })
})

describe('LibraryPage trust gate', () => {
  // The consent flow is reached by clicking Enable on a DISABLED third-party
  // tile, which the Library's default "enabled only" view filters out. This
  // block is about the trust gate, not the default view, so it opts into the
  // show-all view (persisted `mc-apps-library-show-all` toggle, '1' = show all)
  // to render the tile it acts on.
  beforeEach(() => {
    localStorage.setItem('mc-apps-library-show-all', '1')
  })
  afterEach(() => {
    localStorage.removeItem('mc-apps-library-show-all')
  })

  it('routes consent-required enable to the detail disclosure', async () => {
    enableApp.mockRejectedValue(SESSION_APPROVAL_REQUIRED())
    renderPage()
    await clickEnable()

    expect(await screen.findByTestId('detail-route')).toBeTruthy()
    expect(modalTitle()).toBeNull()
  })

  it('opens the consent modal when enable is refused with app_execution_denied', async () => {
    enableApp.mockRejectedValue(TRUST_DENIED())
    renderPage()
    await clickEnable()

    await waitFor(() => expect(modalTitle()).toBeTruthy())
    // Scope disclosure, the three capabilities, and the provenance line.
    // Both copy lines interpolate the app identity. Missing these vars renders
    // the raw `{{app}}` token in the real catalog-backed UI.
    expect(screen.getByText(`${K}.scope LaunchDarkly`)).toBeTruthy()
    expect(screen.getByText(`${K}.intro LaunchDarkly`)).toBeTruthy()
    expect(screen.getByText(`${K}.capability_python`)).toBeTruthy()
    expect(screen.getByText(`${K}.capability_backend`)).toBeTruthy()
    expect(screen.getByText(`${K}.capability_shell`)).toBeTruthy()
    // The session-approval grant sits OUTSIDE the three-row ceiling, in plain
    // words, with the modes listed under their own label.
    expect(screen.getByText(`${K}.session_approval_heading`)).toBeTruthy()
    expect(screen.getByText(`${K}.session_approval_desc`)).toBeTruthy()
    expect(screen.getByText(`${K}.session_approval_modes`)).toBeTruthy()
    expect(screen.queryByText('sessionApproval')).toBeNull()
    expect(screen.getByText('components.approvalModePicker.normal_label')).toBeTruthy()
    expect(screen.getByText('components.approvalModePicker.normal_desc')).toBeTruthy()
    expect(screen.getByText('components.approvalModePicker.reads_label')).toBeTruthy()
    expect(screen.getByText('components.approvalModePicker.trust_label (components.approvalModePicker.chat_mode_hint)')).toBeTruthy()
    // YOLO is process-global and dashboard-only: never offered to an app.
    expect(screen.queryByText('components.approvalModePicker.yolo_label')).toBeNull()
    expect(screen.getByText(`${K}.source`)).toBeTruthy()
    expect(screen.getByText(THIRD_PARTY.trustRepository)).toBeTruthy()
    expect(screen.queryByText(THIRD_PARTY.repo)).toBeNull()
    // The raw backend string never reaches the user.
    expect(screen.queryByText(/is not trusted to run its own code/)).toBeNull()
  })

  it('grants trust for that one app and retries the enable on confirm', async () => {
    enableApp.mockRejectedValueOnce(TRUST_DENIED()).mockResolvedValue({ ok: true })
    renderPage()
    await clickEnable()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(trustApp).toHaveBeenCalledWith(
      THIRD_PARTY.name,
      THIRD_PARTY.trustRepository,
    ))
    await waitFor(() => expect(enableApp).toHaveBeenCalledTimes(2))
    expect(trustApp).toHaveBeenCalledTimes(1)
    // Grant landed and the retry succeeded → the modal closes.
    await waitFor(() => expect(modalTitle()).toBeNull())
  })

  it('fails closed instead of rendering or rewriting embedded clone credentials', async () => {
    const secret = 'SuperSecret'
    const credentialed = `HTTPS://User:${secret}@Git.Example.test/Owner/App.git?Ref=Case#Frag`
    listApps.mockResolvedValueOnce([{
      name: THIRD_PARTY.name,
      displayName: THIRD_PARTY.displayName,
      version: '1.0.0',
      enabled: false,
      origin: 'registry',
      trustRepository: credentialed,
      manifest: { name: THIRD_PARTY.name, version: '1.0.0' },
    }])
    enableApp.mockRejectedValueOnce(TRUST_DENIED()).mockResolvedValue({ ok: true })

    renderPage()
    await clickEnable()
    await waitFor(() => expect(enableApp).toHaveBeenCalledTimes(1))
    expect(modalTitle()).toBeNull()
    expect(screen.queryByText(new RegExp(secret))).toBeNull()
    expect(trustApp).not.toHaveBeenCalled()
  })

  it.each([
    'deploy:ScpSecret@git.example.test:Owner/App.git',
    'deploy:ScpSecret@git.example.test/Owner/App.git',
    ':ScpSecret@git.example.test/Owner/App.git',
  ])('fails closed for an ambiguous colon-bearing SCP consent proof: %s', async credentialed => {
    const secret = 'ScpSecret'
    listApps.mockResolvedValueOnce([{
      name: THIRD_PARTY.name,
      displayName: THIRD_PARTY.displayName,
      version: '1.0.0',
      enabled: false,
      origin: 'registry',
      trustRepository: credentialed,
      manifest: { name: THIRD_PARTY.name, version: '1.0.0' },
    }])
    enableApp.mockRejectedValueOnce(TRUST_DENIED()).mockResolvedValue({ ok: true })

    renderPage()
    await clickEnable()
    await waitFor(() => expect(enableApp).toHaveBeenCalledTimes(1))
    expect(modalTitle()).toBeNull()
    expect(screen.queryByText(new RegExp(secret))).toBeNull()
    expect(trustApp).not.toHaveBeenCalled()
  })

  it.each([
    'ssh://deploy@git.example.test/Owner/App.git',
    'deploy@git.example.test:Owner/App.git',
  ])('preserves the server-reviewed Git routing identity in consent proof: %s', async reviewed => {
    listApps.mockResolvedValueOnce([{
      name: THIRD_PARTY.name,
      displayName: THIRD_PARTY.displayName,
      version: '1.0.0',
      enabled: false,
      origin: 'registry',
      trustRepository: reviewed,
      manifest: { name: THIRD_PARTY.name, version: '1.0.0' },
    }])
    enableApp.mockRejectedValueOnce(TRUST_DENIED()).mockResolvedValue({ ok: true })

    renderPage()
    await clickEnable()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    expect(screen.getByText(reviewed)).toBeTruthy()
    fireEvent.click(confirmBtn())
    await waitFor(() => expect(trustApp).toHaveBeenCalledWith(THIRD_PARTY.name, reviewed))
  })

  it('keeps the modal open and reports inline when the retried enable fails', async () => {
    enableApp.mockRejectedValue(TRUST_DENIED())
    // The rollback probe must not PROVE absence here (a 404 would), so the
    // grant stands and the copy points at Settings.
    getApp.mockRejectedValue(apiError(500, { error: 'gateway exploded' }, 'gateway exploded'))
    renderPage()
    await clickEnable()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    // `toContain`, not `toBe`: the failure renders through the shared ErrorNotice,
    // whose `role="alert"` node also carries the agent hand-off button's label.
    await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('alert').textContent)
      .toContain(`${K}.failed LaunchDarkly`))
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

    await waitFor(() => expect(trustApp).toHaveBeenCalledWith(
      THIRD_PARTY.name,
      THIRD_PARTY.trustRepository,
    ))
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
    // The page loaded from the registry (the suite's 404 default). From here the
    // rollback probe must not prove absence, so the grant stands.
    getApp.mockRejectedValue(apiError(500, { error: 'gateway exploded' }, 'gateway exploded'))

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('alert').textContent)
      .toContain(`${K}.failed LaunchDarkly`))
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
    await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('alert').textContent)
      .toContain(`${K}.failed_generic LaunchDarkly`))
    expect(modalTitle()).toBeTruthy()
  })

  it('KEEPS the grant when absence cannot be proven — only a 404 rolls back', async () => {
    // The other half, and the anti-guess rule. Only a 404 proves the name is
    // unoccupied; a network error or a 500 proves nothing, and revoking on that
    // would switch off an app that exists and works. The suite default is a 404
    // (so the detail page loads from the registry — a non-404 rejection is now
    // a load FAILURE, not "not installed"); the probe is switched to a 500 once
    // the page is up, so this is that branch: the grant stands and the copy
    // points the user at Settings to review it.
    installFromRegistryStream.mockResolvedValue(INSTALL_DENIED())
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())
    getApp.mockRejectedValue(apiError(500, { error: 'gateway exploded' }, 'gateway exploded'))

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('alert').textContent)
      .toContain(`${K}.failed LaunchDarkly`))
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
    await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('alert').textContent)
      .toContain(`${K}.failed_generic LaunchDarkly`))
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

  it('shows the server reason under the headline when the retried install fails', async () => {
    // REGRESSION (#13446): the modal showed `failed_generic` alone and discarded
    // the install stream's error, so a desktop user hit the same dead end on every
    // Try again with the cause visible only in `security_events.jsonl`. The
    // headline stays — it carries the "nothing was changed" advice — and the raw
    // server sentence is the notice's own message, so the agent hand-off keeps it.
    const refusal = 'Python apps that require a build step are not supported in the desktop app'
    installFromRegistryStream
      .mockResolvedValueOnce(INSTALL_DENIED())
      .mockResolvedValue({ ok: false, name: THIRD_PARTY.name, error: refusal })
    getApp.mockRejectedValue(apiError(404, { error: 'app not installed' }))
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => {
      const alert = within(screen.getByRole('dialog')).getByRole('alert').textContent
      expect(alert).toContain(refusal)
      expect(alert).toContain(`${K}.failed_generic LaunchDarkly`)
    })
  })

  it('never renders the trust-denied CODE as the reason', async () => {
    // A second refusal rejects with `app_execution_denied` as a sentinel, not as
    // user-facing text; the modal's own copy explains a grant that did not take
    // effect. Showing the machine code would be a worse dead end than the generic
    // message it replaced.
    installFromRegistryStream.mockResolvedValue(INSTALL_DENIED())
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())
    getApp.mockRejectedValue(apiError(500, { error: 'gateway exploded' }, 'gateway exploded'))

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('alert').textContent)
      .toContain(`${K}.failed LaunchDarkly`))
    expect(within(screen.getByRole('dialog')).getByRole('alert').textContent)
      .not.toContain(APP_EXECUTION_DENIED)
  })
})

describe('retryFailureDetail', () => {
  it('lifts the failure message and drops what is not user-facing', () => {
    expect(retryFailureDetail(new Error('  git clone exploded  '))).toBe('git clone exploded')
    expect(retryFailureDetail('plain string failure')).toBe('plain string failure')
    // A machine code is not a reason to show.
    expect(retryFailureDetail(new Error(APP_EXECUTION_DENIED))).toBe('')
    expect(retryFailureDetail(TRUST_DENIED())).toBe('')
    expect(retryFailureDetail(INSTALL_DENIED())).toBe('')
    // Nothing to show rather than an empty detail beside the headline.
    expect(retryFailureDetail(new Error('   '))).toBe('')
    expect(retryFailureDetail(undefined)).toBe('')
    expect(retryFailureDetail({ ok: false })).toBe('')
  })
})

describe('retryFailureCode', () => {
  it('lifts the code only beside a reason worth showing', () => {
    const permanent = Object.assign(new Error('build refused'), { code: DESKTOP_BUILD_STEP_UNSUPPORTED })
    expect(retryFailureCode(permanent)).toBe(DESKTOP_BUILD_STEP_UNSUPPORTED)
    // A reason with no code selects nothing special.
    expect(retryFailureCode(new Error('git clone exploded'))).toBe('')
    // A sentinel refusal has no reason, so its code has nothing to select.
    expect(retryFailureCode(INSTALL_DENIED())).toBe('')
    expect(retryFailureCode(TRUST_DENIED())).toBe('')
    expect(retryFailureCode(undefined)).toBe('')
  })
})

describe('registry install trust gate — permanent desktop refusal', () => {
  const REFUSAL =
    'Python apps that require a build step are not supported in the desktop app: ' +
    'its bundled interpreter is inside the signed application bundle and cannot install packages'

  beforeEach(() => {
    listRegistry.mockResolvedValue({
      apps: [{ ...THIRD_PARTY, installed: false, enabled: false }],
      serverPlatform: { os: 'darwin', arch: 'arm64' },
    })
    __resetErrorJournalForTests()
  })

  it('drops the retry instruction and explains the refusal in plain words above the server sentence', async () => {
    // The `done` payload names the condition by CODE. The generic headline tells
    // the user to "Choose Try again", which cannot help with a permanent condition,
    // and the server's sentence is developer vocabulary — so the notice becomes
    // the desktop headline, one plain sentence, and the raw sentence beneath.
    installFromRegistryStream
      .mockResolvedValueOnce(INSTALL_DENIED())
      .mockResolvedValue({ ok: false, name: THIRD_PARTY.name, error: REFUSAL, code: DESKTOP_BUILD_STEP_UNSUPPORTED })
    getApp.mockRejectedValue(apiError(404, { error: 'app not installed' }))
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => {
      const alert = within(screen.getByRole('dialog')).getByRole('alert')
      // Named like `failed_generic` names it: the {{app}} threading is observable.
      expect(alert.textContent).toContain(`${K}.failed_desktop_unsupported ${THIRD_PARTY.displayName}`)
      expect(alert.textContent).toContain(`${K}.failed_desktop_unsupported_help`)
      expect(alert.textContent).toContain(REFUSAL)
      expect(alert.textContent).not.toContain(`${K}.failed_generic`)
      // Reading order: headline, plain sentence, then the server's own words.
      const text = alert.textContent ?? ''
      expect(text.indexOf(`${K}.failed_desktop_unsupported_help`)).toBeLessThan(text.indexOf(REFUSAL))
    })
    // The raw sentence is still the journal key, with the code recorded beside it,
    // so the ask-the-agent hand-off keeps the endpoint, the log and the code even
    // though the notice's message is now the plain sentence.
    const report = findReport(REFUSAL)
    expect(report?.code).toBe(DESKTOP_BUILD_STEP_UNSUPPORTED)
    expect(report?.endpoint).toBe('/api/apps/registry/install-stream')
  })

  it('keeps the generic headline for a reason the server did not mark permanent', async () => {
    // A clone or build failure may well be transient, so "Try again" stays.
    installFromRegistryStream
      .mockResolvedValueOnce(INSTALL_DENIED())
      .mockResolvedValue({ ok: false, name: THIRD_PARTY.name, error: 'build failed (exit 1): npm install' })
    getApp.mockRejectedValue(apiError(404, { error: 'app not installed' }))
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => {
      const alert = within(screen.getByRole('dialog')).getByRole('alert').textContent
      expect(alert).toContain('build failed (exit 1): npm install')
      expect(alert).toContain(`${K}.failed_generic`)
      expect(alert).not.toContain(`${K}.failed_desktop_unsupported`)
    })
  })

  it('keeps the landed-grant copy over the desktop headline when the rollback did not happen', async () => {
    // A grant left behind is the more urgent fact: the `failed` copy is what
    // sends the user to Settings to remove it, so it outranks the desktop copy.
    installFromRegistryStream
      .mockResolvedValueOnce(INSTALL_DENIED())
      .mockResolvedValue({ ok: false, name: THIRD_PARTY.name, error: REFUSAL, code: DESKTOP_BUILD_STEP_UNSUPPORTED })
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())
    // The rollback probe cannot prove absence (500, not 404), so the grant stands.
    getApp.mockRejectedValue(apiError(500, { error: 'gateway exploded' }, 'gateway exploded'))

    fireEvent.click(confirmBtn())

    await waitFor(() => {
      const alert = within(screen.getByRole('dialog')).getByRole('alert').textContent
      expect(alert).toContain(`${K}.failed LaunchDarkly`)
      expect(alert).toContain(REFUSAL)
      expect(alert).not.toContain(`${K}.failed_desktop_unsupported`)
    })
  })

  it('offers Close instead of Try again once the refusal is permanent', async () => {
    // "Try again" re-runs `api.trustApp` plus a full clone/build that ends in the
    // identical refusal, then leans on the rollback probe to revoke the grant it
    // just re-wrote. The copy already says retrying cannot help; the footer must
    // not contradict it. The grant was rolled back (404), so closing loses nothing.
    installFromRegistryStream
      .mockResolvedValueOnce(INSTALL_DENIED())
      .mockResolvedValue({ ok: false, name: THIRD_PARTY.name, error: REFUSAL, code: DESKTOP_BUILD_STEP_UNSUPPORTED })
    getApp.mockRejectedValue(apiError(404, { error: 'app not installed' }))
    renderDetailFromGet()
    await waitFor(() => expect(modalTitle()).toBeTruthy())

    fireEvent.click(confirmBtn())

    await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('alert').textContent)
      .toContain(`${K}.failed_desktop_unsupported`))
    const dialog = within(screen.getByRole('dialog'))
    expect(dialog.queryByRole('button', { name: `${K}.confirm_after_failure` })).toBeNull()
    expect(dialog.queryByRole('button', { name: `${K}.cancel` })).toBeNull()
    const streamCalls = installFromRegistryStream.mock.calls.length

    fireEvent.click(dialog.getByRole('button', { name: 'app.close' }))

    await waitFor(() => expect(modalTitle()).toBeNull())
    // Closing is the whole action: nothing was re-run.
    expect(installFromRegistryStream.mock.calls.length).toBe(streamCalls)
  })

  it('selects the same plain copy on the page when an already-trusted app is refused', async () => {
    // An app that is already trusted never opens the consent modal: the install
    // fails on the first attempt and the refusal lands in the page's error box.
    // The `done` payload's code is journaled beside the message, and the box
    // keys its copy on that record — headline and plain sentence — so the
    // trusted path does not fall back to the developer vocabulary alone. The
    // server streams its refusal line into the install log first, as the real
    // gateway does.
    installFromRegistryStream.mockImplementationOnce(async (_name: string, onLine: (line: string) => void) => {
      onLine(`Refusing install: ${REFUSAL}`)
      return { ok: false, name: THIRD_PARTY.name, error: REFUSAL, code: DESKTOP_BUILD_STEP_UNSUPPORTED }
    })
    renderDetailFromGet()

    // The page has other alert regions; the error box is the one leading with
    // the page's headline.
    const box = () => screen.queryAllByRole('alert')
      .find(el => el.textContent?.includes('pages.appDetailPage.install_desktop_unsupported'))
    await waitFor(() => expect(box()).toBeTruthy())
    const text = box()?.textContent ?? ''
    // One verb on both surfaces (the refusal is the install's): the page's
    // headline is its own sentence, not the modal's app-named one.
    expect(text).toContain('pages.appDetailPage.install_desktop_unsupported')
    expect(text).not.toContain(`${K}.failed_desktop_unsupported ${THIRD_PARTY.displayName}`)
    // The shared plain sentence rides inside the page's own trailing one, which
    // names the way back to a retry (the banner's dismiss).
    expect(text).toContain(`pages.appDetailPage.install_desktop_unsupported_help ${K}.failed_desktop_unsupported_help`)
    // The server's own sentence is not a third rendering inside the banner: the
    // log panel beneath shows it verbatim as the streamed line, and the hand-off
    // carries it through the report (still keyed by that sentence).
    expect(text).not.toContain(REFUSAL)
    expect(screen.getByText(`Refusing install: ${REFUSAL}`)).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(findReport(REFUSAL)?.code).toBe(DESKTOP_BUILD_STEP_UNSUPPORTED)

    // One failure surface. The install-log panel is up too, and for any other
    // failure its header is a second red notice with its own hand-off and
    // dismiss; under this banner it folds to a plain title naming the log (not
    // the outcome, which the banner states) with the log still reachable beneath,
    // and the panel's own close goes with it -- the banner's dismiss is the one
    // control.
    const alerts = () => screen.queryAllByRole('alert')
    expect(alerts().filter(el => el.textContent?.includes('pages.appDetailPage.install_failed'))).toHaveLength(0)
    expect(screen.queryByText('pages.appDetailPage.install_failed')).toBeNull()
    expect(screen.getByText('pages.appDetailPage.install_log')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'components.errorNotice.dismiss' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'pages.appDetailPage.close' })).toBeNull()

    // Install under a banner that says installing is impossible would run the
    // same clone and build into the same refusal, so it is disabled while the
    // banner is up; dismissing the banner is how a retry is reached once the
    // author has shipped the packages -- so the dismiss control SAYS so, as
    // visible text that is also its accessible name, instead of an icon-only X
    // whose role the paragraph alone explains, and the disabled button says the
    // same at the point of action (its title). Dismissing clears the log panel
    // too, so no "Install log" (or, once the error is gone, "Install complete")
    // is left standing beside a re-enabled Install, and the title goes with the
    // disabled state.
    const install = () => screen.getByRole('button', { name: 'pages.appDetailPage.install' })
    expect(install()).toBeDisabled()
    expect(install()).toHaveAttribute('title', 'pages.appDetailPage.dismiss_notice_to_install_again')
    const dismissButtons = within(box()!).getAllByRole('button', { name: 'pages.appDetailPage.dismiss_to_install_again' })
    expect(dismissButtons).toHaveLength(1)
    const dismiss = dismissButtons[0]
    expect(dismiss.textContent).toContain('pages.appDetailPage.dismiss_to_install_again')
    expect(dismiss.getAttribute('aria-label')).toBe('pages.appDetailPage.dismiss_to_install_again')
    fireEvent.click(dismiss)
    await waitFor(() => expect(box()).toBeUndefined())
    expect(install()).not.toBeDisabled()
    expect(install()).not.toHaveAttribute('title')
    expect(screen.queryByText('pages.appDetailPage.install_log')).toBeNull()
    expect(screen.queryByText('pages.appDetailPage.install_complete')).toBeNull()
  })

  it('keeps the raw prose on the page for a refusal the server did not mark permanent', async () => {
    installFromRegistryStream
      .mockResolvedValue({ ok: false, name: THIRD_PARTY.name, error: 'build failed (exit 1): npm install' })
    renderDetailFromGet()

    const box = () => screen.getAllByRole('alert')
      .find(el => el.textContent?.includes('build failed (exit 1): npm install'))
    await waitFor(() => expect(box()).toBeTruthy())
    expect(box()?.textContent).not.toContain(`${K}.failed_desktop_unsupported`)
  })
})

describe('safeHref — the provenance link is not a script sink', () => {
  // REGRESSION: repository text is remote content. Rendering it straight into
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

describe('credentialFreeRepository', () => {
  it('strips credentials and suffixes without changing Git routing identity', () => {
    expect(credentialFreeRepository(
      'SSH://Git:SuperSecret@[2001:DB8::A]:2222/Owner/Repo?Ref=Case#Frag',
    )).toBe('SSH://Git@[2001:DB8::A]:2222/Owner/Repo')
    expect(credentialFreeRepository('git@EXAMPLE.COM:Owner/Repo'))
      .toBe('git@EXAMPLE.COM:Owner/Repo')
    expect(credentialFreeRepository('deploy:secret@EXAMPLE.COM:Owner/Repo'))
      .toBeUndefined()
    expect(credentialFreeRepository('deploy:secret@EXAMPLE.COM/Owner/Repo'))
      .toBeUndefined()
    expect(credentialFreeRepository(':secret@EXAMPLE.COM/Owner/Repo'))
      .toBeUndefined()
    expect(credentialFreeRepository(
      'HTTPS://User:SuperSecret@EXAMPLE.COM/Owner/Repo?token=secret#private',
    )).toBe('HTTPS://EXAMPLE.COM/Owner/Repo')
    expect(credentialFreeRepository('/Tmp/user@host/repo')).toBe('/Tmp/user@host/repo')
  })
})
