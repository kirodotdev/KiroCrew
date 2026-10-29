/**
 * SecurityPanel's Registry trust section: registries the operator trusts with the git
 * identity, their grant and revoke flows, and the refusal and damaged-file copy. Split from
 * SecurityPanel.test.tsx, whose api mock this file copies; the fixtures they share are in
 * src/test/SecurityPanelTestUtils.tsx.
 */
import { ApiError } from '../../api/client'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* ── api client mock ───────────────────────────────────────────────────────
 * SecurityPanel drives all its mutations through the `api` client methods.  We
 * mock those methods so no network I/O happens; each returns the (mutated)
 * snapshot the panel then re-renders from.  `securityPosture` feeds the Live
 * Security Posture card, and `governancePolicy` feeds the ceiling viewer.
 */
vi.mock('../../api/client', () => ({
  // The real class, not a stub: `trustFailureMessage` branches on `instanceof
  // ApiError` to decide whether a structured body is available to read.
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
  api: {
    deniedCommands: vi.fn(),
    toggleBuiltinDeniedCommand: vi.fn(),
    setDeniedCommandsDisableAll: vi.fn(),
    addUserDeniedCommand: vi.fn(),
    toggleUserDeniedCommand: vi.fn(),
    deleteUserDeniedCommand: vi.fn(),
    redactionAllowedHosts: vi.fn(),
    redactionRevokeHost: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    // Read + write for the third-party-app execution toggle. Also consumed by
    // YoloDurationCard, which tolerates an unresolved read.
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    // Read by the rail on every mount to summarise the tailnet-origin section.
    // Present here so the rail's own read is a resolved query rather than a
    // crash on an undefined queryFn; the section's behaviour is covered in
    // SecurityPanel.tailnet.test.tsx.
    tailnetStatus: vi.fn(),
    listTrustedApps: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    setTrustAllApps: vi.fn(),
    // Read + the two writes behind the registry git-identity trust section.
    listTrustedRegistries: vi.fn(),
    grantTrustedRegistry: vi.fn(),
    revokeTrustedRegistry: vi.fn(),
  },
}))

import { api } from '../../api/client'
import type { TrustedRegistriesData } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'
import { i18nT } from '../../i18n/t'
import { TAILNET_OFF, snapshot, posture, govNoPolicy } from '../../test/SecurityPanelTestUtils'

/** Registry-trust copy, resolved the way the panel resolves it.
 *
 * Same rationale as the `T` block in SecurityPanel.test.tsx: asserting the resolved key pins the
 * BEHAVIOUR (this control is wired to that key) rather than a literal English
 * string the catalog owns. */
const RK = 'pages.settings.securityPanel.trustedRegistries'
const R = {
  // `empty` is a <Trans> value with a <link>…</link> run, so its resolved key
  // carries markup the DOM never shows. Match the lead sentence the panel does
  // render, and assert the pointer link separately (emptyLink below).
  empty: () => 'No registries added by hand.',
  grant: () => i18nT(`${RK}.grant`),
  revoke: () => i18nT(`${RK}.revoke`),
  trustedBadge: () => i18nT(`${RK}.trusted_badge`),
  notServed: () => i18nT(`${RK}.not_served`),
  // Rendered through <Trans> with an <apps> link; match its plain text.
  notServedPinned: () => i18nT(`${RK}.not_served_pinned_name`).replace(/<\/?apps>/g, ''),
  notServedNotConfigured: () => i18nT(`${RK}.not_served_not_configured`),
  rowMeta: (host: string, branch: string) => i18nT(`${RK}.row_meta`, { host, branch }),
  grantConfirmBody: (name: string) => i18nT(`${RK}.grant_confirm_body`, { name }),
  // Rendered through <Trans> with the URL in its own <url> span; match the plain text.
  grantConfirmRepo: (repo: string) => i18nT(`${RK}.grant_confirm_repo`, { repo }).replace(/<\/?url>/g, ''),
  grantConfirmOk: () => i18nT(`${RK}.grant_confirm_ok`),
  changeFailed: (detail: string) => i18nT(`${RK}.change_failed`, { detail }),
  unavailable: () => i18nT(`${RK}.unavailable`),
  retry: () => i18nT(`${RK}.retry`),
  corruptNotice: (path = 'registry_trust.json') => i18nT(`${RK}.corrupt_notice`, { path }),
  cancel: () => i18nT('pages.settings.securityPanel.cancel'),
}

const PRIVATE_REPO = 'https://github.example.com/platform/app-registry'
const PUBLIC_REPO = 'https://github.com/org/community-registry'

/** Two hand-added registries: one untrusted (offers Grant), one already
 *  trusted (offers Revoke), so both arms render from the same snapshot. */
function registries(overrides: Partial<TrustedRegistriesData> = {}): TrustedRegistriesData {
  return {
    registries: [
      {
        name: 'Platform registry',
        repo: PRIVATE_REPO,
        branch: 'main',
        host: 'github.example.com',
        trusted: false,
        granted: false,
        served: true,
      },
      {
        name: 'Community registry',
        repo: PUBLIC_REPO,
        branch: 'release',
        host: 'github.com',
        trusted: true,
        granted: true,
        served: true,
      },
    ],
    ...overrides,
  }
}

// The row testid is now `trusted-registry-<repo>#<name>` (name suffix added so
// two config rows tracking one repo at two branches get their own key/testid).
// Match a row by its repo prefix, so a test does not have to spell the name.
const regRowMatcher = (repo: string) => (id: string | null) =>
  typeof id === 'string' && id.startsWith(`trusted-registry-${repo}#`)
const findRegRow = (repo: string) => screen.findByTestId(regRowMatcher(repo))
const getRegRow = (repo: string) => screen.getByTestId(regRowMatcher(repo))

/* ── Registry git-identity trust ────────────────────────────────────────────
 * A hand-added registry clones without credentials, so an app in a private
 * repository shows no art and cannot be installed. Granting trust hands that
 * registry the operator's own ssh keys for the repositories IT chooses to list,
 * which is why the grant is confirmed and the revoke is not.
 */

describe('SecurityPanel — registries trusted with the git identity', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue(registries())
    ;(api.grantTrustedRegistry as ReturnType<typeof vi.fn>).mockResolvedValue(
      registries({
        registries: registries().registries.map(row =>
          row.repo === PRIVATE_REPO
            ? { ...row, trusted: true, granted: true }
            : row),
      }),
    )
    ;(api.revokeTrustedRegistry as ReturnType<typeof vi.fn>).mockResolvedValue(
      registries({
        registries: registries().registries.map(row =>
          row.repo === PUBLIC_REPO ? { ...row, trusted: false, granted: false } : row),
      }),
    )
  })

  it('renders one row per configured registry with its host and branch', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PRIVATE_REPO)
    expect(within(row).getByText('Platform registry')).toBeInTheDocument()
    // The row shows the repository path, so two registries on one forge differ.
    expect(within(row).getByText(R.rowMeta('github.example.com/platform/app-registry', 'main'))).toBeInTheDocument()
    // Untrusted → offers the grant, carries no badge.
    expect(within(row).getByRole('button', { name: R.grant() })).toBeInTheDocument()
    expect(within(row).queryByText(R.trustedBadge())).not.toBeInTheDocument()

    // The trusted row is the other arm of the same snapshot: badge + revoke.
    const trusted = getRegRow(PUBLIC_REPO)
    expect(within(trusted).getByText(R.trustedBadge())).toBeInTheDocument()
    expect(within(trusted).getByRole('button', { name: R.revoke() })).toBeInTheDocument()
    expect(within(trusted).queryByRole('button', { name: R.grant() })).not.toBeInTheDocument()

    // With rows present, the empty state must NOT also be on screen.
    expect(screen.queryByText(R.empty(), { exact: false })).not.toBeInTheDocument()
  })

  it('renders both rows when two config rows track one repo at different names/branches', async () => {
    // Two config rows can name the SAME repository at two branches. Their key and
    // testid carry the row name too (`<repo>#<name>`), so both render with their
    // own identity instead of colliding on the repo alone.
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue({
      registries: [
        { name: 'Platform main', repo: PRIVATE_REPO, branch: 'main', host: 'github.example.com', trusted: false, granted: false, served: true },
        { name: 'Platform dev', repo: PRIVATE_REPO, branch: 'dev', host: 'github.example.com', trusted: false, granted: false, served: true },
      ],
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const mainRow = await screen.findByTestId(`trusted-registry-${PRIVATE_REPO}#Platform main`)
    const devRow = screen.getByTestId(`trusted-registry-${PRIVATE_REPO}#Platform dev`)
    // Each renders its own name, and they are distinct nodes (no key collision).
    expect(within(mainRow).getByText('Platform main')).toBeInTheDocument()
    expect(within(devRow).getByText('Platform dev')).toBeInTheDocument()
    expect(mainRow).not.toBe(devRow)
  })

  it('granting confirms BEFORE mutating, and posts the credential-free repo URL', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PRIVATE_REPO)
    fireEvent.click(within(row).getByRole('button', { name: R.grant() }))

    // Nothing is granted on the first click: who chooses which of the reader's
    // private repositories get cloned has to be on screen before it happens.
    expect(api.grantTrustedRegistry).not.toHaveBeenCalled()
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(R.grantConfirmBody('Platform registry'))).toBeInTheDocument()
    // The name is index content the registry's author controls, so the dialog
    // must also show the URL the grant is keyed on. Asserted against the RAW
    // url, not the resolved key: a catalog that dropped {{repo}} would still
    // satisfy the key-resolved form while showing nothing the operator typed.
    expect(dialog.textContent).toContain(R.grantConfirmRepo(PRIVATE_REPO))
    // The URL alone is monospace and wraps at any point, never mid-label.
    expect(within(dialog).getByText(PRIVATE_REPO)).toHaveClass('font-mono')

    fireEvent.click(within(dialog).getByRole('button', { name: R.grantConfirmOk() }))
    // The REPO is the identity, never the display name — the name is index
    // content the registry's author controls.
    await waitFor(() => expect(api.grantTrustedRegistry).toHaveBeenCalledWith(PRIVATE_REPO))

    // The returned snapshot is applied, so the row flips without a second read.
    const granted = await findRegRow(PRIVATE_REPO)
    expect(within(granted).getByText(R.trustedBadge())).toBeInTheDocument()
    expect(within(granted).getByRole('button', { name: R.revoke() })).toBeInTheDocument()
    expect(api.listTrustedRegistries).toHaveBeenCalledTimes(1)
  })

  it('cancelling the grant confirm mutates nothing', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PRIVATE_REPO)
    fireEvent.click(within(row).getByRole('button', { name: R.grant() }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: R.cancel() }))

    expect(api.grantTrustedRegistry).not.toHaveBeenCalled()
  })

  it('revoking is immediate — it narrows what someone else decides', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PUBLIC_REPO)
    fireEvent.click(within(row).getByRole('button', { name: R.revoke() }))

    await waitFor(() => expect(api.revokeTrustedRegistry).toHaveBeenCalledWith(PUBLIC_REPO))
    // No confirm: demanding one for the safe direction is what trains people to
    // click through the dangerous one.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    const revoked = await findRegRow(PUBLIC_REPO)
    expect(within(revoked).queryByText(R.trustedBadge())).not.toBeInTheDocument()
    expect(within(revoked).getByRole('button', { name: R.grant() })).toBeInTheDocument()
  })

  it('renders the empty state, with a pointer to where a registry is added', async () => {
    // Build-pinned registries are never in this payload, so an install with only
    // those reads as empty here — which is true: nothing is the operator's to grant.
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue({ registries: [] })
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    expect(await screen.findByText(R.empty(), { exact: false })).toBeInTheDocument()
    expect(screen.queryByText(R.trustedBadge())).not.toBeInTheDocument()
    // The empty state is not a dead end: it links to the App Store's Apps page,
    // where the registry-sources manager lives, so an operator with nothing to
    // grant is told where to add a registry.
    const addLink = screen.getByRole('link', { name: 'Apps' })
    expect(addLink).toHaveAttribute('href', '/apps')
  })

  it('surfaces the backend reason when the grant is refused', async () => {
    // A 400 names the repo the backend does not know about, which is the only
    // actionable part: collapsing it into "something went wrong" hides it.
    const detail = 'repo is not a configured registry'
    ;(api.grantTrustedRegistry as ReturnType<typeof vi.fn>).mockRejectedValue(
      new ApiError(400, 'Bad request', JSON.stringify({ error: detail })),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PRIVATE_REPO)
    fireEvent.click(within(row).getByRole('button', { name: R.grant() }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: R.grantConfirmOk() }))

    expect(await screen.findByText(R.changeFailed(detail))).toBeInTheDocument()
    // The row must NOT claim the grant landed.
    const unchanged = getRegRow(PRIVATE_REPO)
    expect(within(unchanged).queryByText(R.trustedBadge())).not.toBeInTheDocument()
  })

  it('a FAILED read says so and offers no grant button', async () => {
    // UNKNOWN is not "untrusted". A Grant button on an unread snapshot would
    // offer to widen a decision whose current state we could not establish.
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    expect(await screen.findByText(R.unavailable())).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: R.grant() })).toBeNull()
    expect(screen.queryByText(R.empty(), { exact: false })).not.toBeInTheDocument()
  })

  it('hides the cached rows under the unavailable notice, and shows them when healthy', async () => {
    // The real bug: a refetch that FAILS after a successful load leaves react-query
    // holding the prior rows while `isError` flips true. Those rows still show a
    // Trusted badge whose truth we can no longer confirm, so the list is hidden
    // while `unavailable` is set — only the notice and its Retry remain.
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue(registries())
    const { queryClient } = renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })
    // Healthy first: rows render (negative control).
    expect(await findRegRow(PRIVATE_REPO)).toBeInTheDocument()
    expect(getRegRow(PUBLIC_REPO)).toBeInTheDocument()
    expect(screen.queryByText(R.unavailable())).not.toBeInTheDocument()

    // The next read fails; the cached rows stay in the query but must be hidden.
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    await queryClient.refetchQueries({ queryKey: ['trusted-registries'] })

    expect(await screen.findByText(R.unavailable())).toBeInTheDocument()
    expect(screen.queryByTestId(regRowMatcher(PRIVATE_REPO))).toBeNull()
    expect(screen.queryByTestId(regRowMatcher(PUBLIC_REPO))).toBeNull()
  })

  it('a grant invalidates the [registries] cache RegistryManager reads', async () => {
    // RegistryManager caches the registries list (staleTime 30s) with
    // trust: "owner"; if the grant leaves it stale, that tier is echoed back
    // into PUT /api/apps/registries and the whole save is denied. The grant
    // must invalidate ['registries'] (and ['apps']) so the manager refetches.
    const { queryClient } = renderWithProviders(<SecurityPanel />, {
      route: '/?section=registries',
    })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    const row = await findRegRow(PRIVATE_REPO)
    fireEvent.click(within(row).getByRole('button', { name: R.grant() }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: R.grantConfirmOk() }))

    await waitFor(() => expect(api.grantTrustedRegistry).toHaveBeenCalledWith(PRIVATE_REPO))
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['registries'] }),
    )
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['apps'] })
  })

  it('a revoke invalidates the [registries] cache too', async () => {
    const { queryClient } = renderWithProviders(<SecurityPanel />, {
      route: '/?section=registries',
    })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    const row = await findRegRow(PUBLIC_REPO)
    fireEvent.click(within(row).getByRole('button', { name: R.revoke() }))

    await waitFor(() => expect(api.revokeTrustedRegistry).toHaveBeenCalledWith(PUBLIC_REPO))
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['registries'] }),
    )
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['apps'] })
  })

  it('a not-served row shows the reason note and never the Trusted badge', async () => {
    // A row the merge drops (here a build-pinned name contest) is served by
    // neither claimant: `served: false` with a reason. The badge follows
    // `served`, so even a stored grant (`trusted` already false from the
    // backend) must render as Not served, no Trusted badge.
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue({
      registries: [
        {
          name: 'Contested registry',
          repo: PRIVATE_REPO,
          branch: 'dev',
          host: 'github.example.com',
          trusted: false,
          granted: false,
          served: false,
          not_served_reason: 'pinned_name',
        },
      ],
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PRIVATE_REPO)
    expect(row.textContent).toContain(R.notServedPinned())
    expect(within(row).getByRole('link')).toHaveAttribute('href', '/apps')
    expect(within(row).queryByText(R.trustedBadge())).not.toBeInTheDocument()
  })

  it('a not-served, UNGRANTED row offers NO trust controls, trusted-flag or not', async () => {
    // A dropped row that holds no grant shows nothing: Grant is gated on
    // `served !== false` (this row is dropped) and Revoke on `granted` (this row
    // has none). It is fixed by renaming/removing it in the registries editor.
    // Even a stale `trusted: true` on a dropped-but-ungranted row (a hand-crafted
    // or older snapshot) still shows nothing — the controls key on served+granted,
    // not on the trusted flag. (A dropped row that IS granted offers Revoke; that
    // is the sibling test below.)
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue({
      registries: [
        {
          name: 'Dropped untrusted',
          repo: PRIVATE_REPO,
          branch: 'main',
          host: 'github.example.com',
          trusted: false,
          granted: false,
          served: false,
          not_served_reason: 'name_collision',
        },
        {
          name: 'Dropped but flagged trusted',
          repo: PUBLIC_REPO,
          branch: 'main',
          host: 'github.com',
          trusted: true,
          granted: false,
          served: false,
          not_served_reason: 'pinned_name',
        },
      ],
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const untrusted = await findRegRow(PRIVATE_REPO)
    expect(within(untrusted).queryByRole('button', { name: R.grant() })).toBeNull()
    expect(within(untrusted).queryByRole('button', { name: R.revoke() })).toBeNull()

    // Even a not-served row flagged trusted shows NO controls: Grant is gated on
    // served (dropped), Revoke on granted (none) — the trusted flag decides neither.
    const flagged = getRegRow(PUBLIC_REPO)
    expect(within(flagged).queryByRole('button', { name: R.grant() })).toBeNull()
    expect(within(flagged).queryByRole('button', { name: R.revoke() })).toBeNull()
  })

  it('a not-served but GRANTED row offers Revoke so a dormant grant can be cleared', async () => {
    // The whole point of the granted field: a grant that went dormant because the
    // merge dropped the row (an operator added a same-name registry) re-arms when
    // the collision resolves, so the panel must still offer Revoke — even though
    // the row is not served and carries no Trusted badge.
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue({
      registries: [
        {
          name: 'Dropped but granted',
          repo: PRIVATE_REPO,
          branch: 'main',
          host: 'github.example.com',
          trusted: false,
          granted: true,
          served: false,
          not_served_reason: 'name_collision',
        },
      ],
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PRIVATE_REPO)
    // Revoke is offered; Grant is not; and it never badges Trusted.
    expect(within(row).getByRole('button', { name: R.revoke() })).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: R.grant() })).toBeNull()
    expect(within(row).queryByText(R.trustedBadge())).not.toBeInTheDocument()

    // Revoke is immediate (it narrows), and posts the row's repo.
    fireEvent.click(within(row).getByRole('button', { name: R.revoke() }))
    await waitFor(() => expect(api.revokeTrustedRegistry).toHaveBeenCalledWith(PRIVATE_REPO))
  })

  it('a served but UNGRANTED row offers Grant, not Revoke', async () => {
    // Negative control on the gating: a served row with no stored grant is the
    // ordinary Grant case, and Revoke must not appear.
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue({
      registries: [
        {
          name: 'Fresh registry',
          repo: PRIVATE_REPO,
          branch: 'main',
          host: 'github.example.com',
          trusted: false,
          granted: false,
          served: true,
        },
      ],
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PRIVATE_REPO)
    expect(within(row).getByRole('button', { name: R.grant() })).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: R.revoke() })).toBeNull()
  })

  it('a grant refused with code corrupt refetches the snapshot and adds no second notice', async () => {
    // onError must invalidate ['trusted-registries'] so a keystone that became
    // corrupt surfaces the card's damaged-file notice immediately; the failure
    // itself adds no change_failed copy, so the problem is shown once.
    ;(api.grantTrustedRegistry as ReturnType<typeof vi.fn>).mockRejectedValue(
      new ApiError(
        500,
        'Server error',
        JSON.stringify({ error: 'registry_trust.json is not valid JSON: line 1', code: 'corrupt' }),
      ),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PRIVATE_REPO)
    // The initial list read has happened once.
    expect(api.listTrustedRegistries).toHaveBeenCalledTimes(1)

    fireEvent.click(within(row).getByRole('button', { name: R.grant() }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: R.grantConfirmOk() }))

    // The snapshot was refetched — the query ran a second time on the failure.
    await waitFor(() => expect(api.listTrustedRegistries).toHaveBeenCalledTimes(2))
    expect(screen.queryByText(R.changeFailed(R.corruptNotice()))).not.toBeInTheDocument()
    expect(screen.queryByText(/registry_trust\.json is not valid JSON/)).not.toBeInTheDocument()
  })

  it('a grant refused with code unknown_registry shows the short removed copy', async () => {
    ;(api.grantTrustedRegistry as ReturnType<typeof vi.fn>).mockRejectedValue(
      new ApiError(
        400,
        'Bad request',
        JSON.stringify({ error: 'repo is not one of the configured registries', code: 'unknown_registry' }),
      ),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })
    const row = await findRegRow(PRIVATE_REPO)
    fireEvent.click(within(row).getByRole('button', { name: R.grant() }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: R.grantConfirmOk() }))
    expect(
      await screen.findByText(R.changeFailed(i18nT(`${RK}.unknown_registry`))),
    ).toBeInTheDocument()
    expect(screen.queryByText(/repo is not one of the configured registries/)).not.toBeInTheDocument()
  })

  it('a grant refused with code not_served shows the not-listed copy', async () => {
    ;(api.grantTrustedRegistry as ReturnType<typeof vi.fn>).mockRejectedValue(
      new ApiError(
        400,
        'Bad request',
        JSON.stringify({ error: 'this registry is not listed: name_collision', code: 'not_served', reason: 'name_collision' }),
      ),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PRIVATE_REPO)
    fireEvent.click(within(row).getByRole('button', { name: R.grant() }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: R.grantConfirmOk() }))

    expect(await screen.findByText(R.changeFailed(R.notServed()))).toBeInTheDocument()
  })

  it('a grant refused with an UNKNOWN code still shows the backend detail', async () => {
    // Negative control: only the known codes map to card copy; anything else
    // falls through to the backend detail so an actionable message is not lost.
    const detail = 'this registry uses a plaintext transport'
    ;(api.grantTrustedRegistry as ReturnType<typeof vi.fn>).mockRejectedValue(
      new ApiError(400, 'Bad request', JSON.stringify({ error: detail, code: 'insecure_transport' })),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PRIVATE_REPO)
    fireEvent.click(within(row).getByRole('button', { name: R.grant() }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: R.grantConfirmOk() }))

    expect(await screen.findByText(R.changeFailed(detail))).toBeInTheDocument()
  })

  it('a corrupt trust file shows the damage notice and no Reset control', async () => {
    // Design 0de17a336bf1: a corrupt keystone must be visible, not a
    // healthy-looking untrusted list. FP da1f705bb604: no product writer makes a
    // corrupt file, so there is no in-app reset — the operator fixes it by hand.
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue(
      registries({ corrupt: true, corrupt_detail: 'registry_trust.json is not valid JSON' }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    expect(await screen.findByText(R.corruptNotice())).toBeInTheDocument()
    expect(screen.queryByTestId('trusted-registries-reset')).not.toBeInTheDocument()
    // Every grant/revoke is refused on a damaged file, so no control is offered.
    expect(screen.queryByRole('button', { name: R.grant() })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: R.revoke() })).not.toBeInTheDocument()
  })

  it('the damaged-file notice names the exact file to delete', async () => {
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue(
      registries({ corrupt: true, corrupt_detail: 'bad json', corrupt_path: '/srv/kc/registry_trust.json' }),
    )
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })
    expect(await screen.findByText(R.corruptNotice('/srv/kc/registry_trust.json'))).toBeInTheDocument()
  })

  it('a healthy trust file shows neither the damage notice nor the Reset control', async () => {
    // Negative control: the default healthy snapshot carries no corrupt flag.
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    await findRegRow(PRIVATE_REPO)
    expect(screen.queryByText(R.corruptNotice())).not.toBeInTheDocument()
    expect(screen.queryByTestId('trusted-registries-reset')).not.toBeInTheDocument()
  })

  it('an orphan grant (not_configured) shows the not-configured note and only Revoke', async () => {
    // Design a453f1137c98: a stored grant whose config row was deleted by hand is
    // surfaced as its own row — served:false, not_served_reason:not_configured,
    // granted:true — so the otherwise-invisible grant can be cleared. It shows the
    // not-configured copy and, because it is granted, a Revoke (never Grant, never
    // a Trusted badge).
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue({
      registries: [
        {
          name: PRIVATE_REPO,
          repo: PRIVATE_REPO,
          branch: '',
          host: 'github.example.com',
          trusted: false,
          granted: true,
          served: false,
          not_served_reason: 'not_configured',
        },
      ],
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    const row = await findRegRow(PRIVATE_REPO)
    expect(within(row).getByText(R.notServedNotConfigured())).toBeInTheDocument()
    // No branch: the meta line is the repository path alone, no dangling clause.
    expect(within(row).getByText('github.example.com/platform/app-registry')).toBeInTheDocument()
    expect(row.textContent).not.toContain('· branch')
    expect(within(row).getByRole('button', { name: R.revoke() })).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: R.grant() })).toBeNull()
    expect(within(row).queryByText(R.trustedBadge())).not.toBeInTheDocument()
    // The wrong not-served notes are not shown.
    expect(within(row).queryByText(R.notServedPinned())).not.toBeInTheDocument()

    // Revoke posts the orphan's repo, clearing the dormant grant.
    fireEvent.click(within(row).getByRole('button', { name: R.revoke() }))
    await waitFor(() => expect(api.revokeTrustedRegistry).toHaveBeenCalledWith(PRIVATE_REPO))
  })

  it('the unavailable notice carries a Retry that refetches the snapshot', async () => {
    // FP 4ed7e2eefdfc: a failed read must offer a Retry that refetches the query
    // in place rather than forcing a page reload. The read fails first (notice +
    // Retry), then the next read succeeds and the rows render.
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('boom'))
    ;(api.listTrustedRegistries as ReturnType<typeof vi.fn>).mockResolvedValue(registries())
    renderWithProviders(<SecurityPanel />, { route: '/?section=registries' })

    expect(await screen.findByText(R.unavailable())).toBeInTheDocument()
    const retry = screen.getByTestId('trusted-registries-retry')
    expect(retry).toHaveTextContent(R.retry())
    expect(api.listTrustedRegistries).toHaveBeenCalledTimes(1)

    fireEvent.click(retry)
    // The refetch ran, and the rows now render in place — no reload.
    await waitFor(() => expect(api.listTrustedRegistries).toHaveBeenCalledTimes(2))
    expect(await findRegRow(PRIVATE_REPO)).toBeInTheDocument()
    expect(screen.queryByText(R.unavailable())).not.toBeInTheDocument()
  })
})
