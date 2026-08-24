import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import type { AwsAccountsResponse, AvailableProfilesResponse } from './types'

/* ── AWS Control api client mock ──────────────────────────────────────────
 * The page reads only through these two methods, so mocking them keeps every
 * case network-free. `AwsControlError` is the real class so `instanceof` and
 * `.status` behave as in production. */
vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    awsControlApi: {
      accounts: vi.fn(),
      reconnectPlan: vi.fn(),
      availableProfiles: vi.fn(),
      registerProfiles: vi.fn(),
    },
  }
})

/* The two paid-service gates fetch their own consent status through the shared
 * client; stub it so they mount without hitting the network. */
vi.mock('../../api/client', () => ({
  api: {
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
    revokeAwsConsent: vi.fn(),
  },
}))

import { awsControlApi, AwsControlError } from './api'
import { api } from '../../api/client'
import AwsControlPage from './AwsControlPage'

function accountsPayload(overrides: Partial<AwsAccountsResponse> = {}): AwsAccountsResponse {
  return {
    accounts: [
      {
        account: '111122223333',
        name: 'personal',
        health: 'ok',
        profiles: [
          {
            name: 'personal', region: 'us-west-2', kind: 'sso', identityOk: true,
            account: '111122223333', arn: 'arn:aws:iam::111122223333:role/x', detail: '', default: true,
          },
        ],
        summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
      },
      {
        account: '444455556666',
        name: 'work',
        health: 'degraded',
        profiles: [
          {
            name: 'work', region: 'eu-west-1', kind: 'credential-process', identityOk: false,
            account: '444455556666', arn: '', detail: 'expired', default: true,
          },
        ],
        summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
      },
    ],
    totals: { accounts: 2, profiles: 2, profilesHealthy: 1 },
    generatedAt: '2026-08-24T05:00:00Z',
    ...overrides,
  }
}

function availablePayload(
  overrides: Partial<AvailableProfilesResponse> = {},
): AvailableProfilesResponse {
  return {
    profiles: [
      { name: 'personal', registered: true },
      { name: 'staging', registered: false },
      { name: 'sandbox', registered: false },
    ],
    registeredCount: 1,
    max: 50,
    supported: true,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  // Keep the consent gates quiet: a never-resolving probe leaves them rendering
  // nothing (the component returns null until its query succeeds), which is fine
  // for the assertions here — we only need the page around them to mount.
  vi.mocked(api.awsConsent).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.awsConsent>)
  // Default the Add-accounts probe so cases that don't exercise it still mount
  // its query without an unhandled rejection.
  vi.mocked(awsControlApi.availableProfiles).mockResolvedValue(availablePayload())
})

describe('AwsControlPage', () => {
  it('renders one thin row per account: name, full id, health dot, keys summary', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    const rows = await screen.findAllByTestId('account-card')
    expect(rows).toHaveLength(2)

    const dots = screen.getAllByTestId('health-dot')
    expect(dots.map((d) => d.getAttribute('data-health'))).toEqual(['ok', 'degraded'])

    // Rows lead with the account name.
    expect(screen.getByText('personal')).toBeTruthy()
    expect(screen.getByText('work')).toBeTruthy()
    // The FULL 12-digit id renders (never a truncated "···1337" tail).
    const ids = screen.getAllByTestId('account-id').map((n) => n.textContent)
    expect(ids).toContain('111122223333')
    expect(ids).toContain('444455556666')
    expect(screen.queryByText(/···/)).toBeNull()
    // Per-row keys summary.
    expect(screen.getAllByTestId('account-keys')[0]).toHaveTextContent(
      i18nT('apps.awsControl.page.keys_summary', { count: 1 }),
    )
  })

  it('shows a single quiet aggregate line, never fake zeros', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    // The line exists during loading as a nbsp placeholder, so wait for the
    // loaded rows first, then read the populated aggregate line.
    await screen.findAllByTestId('account-id')
    const line = screen.getByTestId('aggregate-line')
    // "2 accounts · 2 keys · 1 healthy" — counts we actually have, joined by · .
    expect(line).toHaveTextContent(i18nT('apps.awsControl.page.aggregate_accounts', { count: 2 }))
    expect(line).toHaveTextContent(i18nT('apps.awsControl.page.aggregate_keys', { count: 2 }))
    expect(line).toHaveTextContent(i18nT('apps.awsControl.page.aggregate_healthy', { count: 1 }))
    // The old 4-card stat strip (and its em-dash "Stored"/"This month" fakes) is gone.
    expect(screen.queryByTestId('totals-strip')).toBeNull()
    // No stored/cost figure is invented as a zero on this surface.
    expect(line.textContent).not.toMatch(/\$?0(\s|$|GB)/)
  })

  it('rows carry no Reconnect action — they only navigate', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    await screen.findByTestId('accounts-list')
    // Reconnect moved to the console's Connections section.
    expect(screen.queryByTestId('reconnect-toggle')).toBeNull()
    expect(screen.queryByTestId('profile-chip')).toBeNull()
  })

  it('an UNRESOLVED row toggles inline Reconnect instead of being a dead row', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload({
      accounts: [
        {
          account: '',
          name: '',
          health: 'unknown',
          profiles: [
            {
              name: 'stale-profile', region: 'us-west-2', kind: 'sso', identityOk: false,
              account: '', arn: '', detail: 'token expired', default: false,
            },
          ],
          summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
        },
      ],
      totals: { accounts: 1, profiles: 1, profilesHealthy: 0 },
    }))
    vi.mocked(awsControlApi.reconnectPlan).mockResolvedValue({
      kind: 'sso', command: 'aws sso login --profile stale-profile',
    })
    renderWithProviders(<AwsControlPage />)

    const row = await screen.findByTestId('account-card')
    // No console exists for an unresolved account: the click opens guidance, not a dead end.
    fireEvent.click(row)
    const panel = await screen.findByTestId('row-reconnect')
    fireEvent.click(within(panel).getByTestId('reconnect-toggle'))
    await screen.findByTestId('reconnect-command')
    expect(screen.getByTestId('reconnect-command')).toHaveTextContent('aws sso login --profile stale-profile')
    // Toggling the row again collapses the guidance.
    fireEvent.click(row)
    expect(screen.queryByTestId('row-reconnect')).toBeNull()
  })

  it('filters the list client-side by name or id', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    await screen.findByTestId('accounts-list')
    fireEvent.change(screen.getByTestId('accounts-search'), { target: { value: '4444' } })

    const rows = screen.getAllByTestId('account-card')
    expect(rows).toHaveLength(1)
    expect(screen.getByText('work')).toBeTruthy()
    expect(screen.queryByText('personal')).toBeNull()

    // A query that matches nothing shows the search-empty line, not the list.
    fireEvent.change(screen.getByTestId('accounts-search'), { target: { value: 'zzz' } })
    expect(screen.getByTestId('accounts-search-empty')).toBeTruthy()
    expect(screen.queryByTestId('accounts-list')).toBeNull()
  })

  it('shows a friendly empty state when there are no accounts', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload({ accounts: [], totals: { accounts: 0, profiles: 0, profilesHealthy: 0 } }))
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('aws-control-empty')).toBeTruthy()
    expect(screen.queryByTestId('account-card')).toBeNull()
  })

  it('mounts both paid-service consent gates (s3 and ce)', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    // The gates read their status through the mocked client — one call per service.
    await waitFor(() => {
      expect(api.awsConsent).toHaveBeenCalledWith('s3')
      expect(api.awsConsent).toHaveBeenCalledWith('ce')
    })
    expect(screen.getByTestId('paid-services')).toBeTruthy()
  })

  it('states each paid-service card is scoped to its named connection, not the whole page', async () => {
    // Defect 2: AwsConsentGate resolves ONE (default) connection internally, so
    // on a multi-account page the cards silently concern a single account. The
    // scope sentence is what stops the operator reading a one-account
    // confirmation as covering every account in the header.
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    const section = await screen.findByTestId('paid-services')
    expect(within(section).getByTestId('paid-services-scope')).toHaveTextContent(
      i18nT('apps.awsControl.page.paid_services_scope'),
    )
  })

  it('keeps the paid-services section visible in the normal (unfiltered) state', async () => {
    // The guard must only hide the section on a no-match search — a plain loaded
    // page with no query still shows it.
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    await screen.findByTestId('accounts-list')
    expect(screen.getByTestId('paid-services')).toBeTruthy()
  })

  it('hides the paid-services section when a search filters every account out', async () => {
    // Defect 1: after a search matches nothing the page already shows
    // "No accounts match X"; rendering the two consent cards next to it reads as
    // if they survived the filter. The section must disappear with the list.
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    await screen.findByTestId('accounts-list')
    expect(screen.getByTestId('paid-services')).toBeTruthy()

    fireEvent.change(screen.getByTestId('accounts-search'), { target: { value: 'no-such-account' } })

    // The no-match line is shown, the list is gone, and so is the paid section.
    expect(screen.getByTestId('accounts-search-empty')).toBeTruthy()
    expect(screen.queryByTestId('accounts-list')).toBeNull()
    expect(screen.queryByTestId('paid-services')).toBeNull()
  })

  it('renders the standard disabled-app state on a 403 app_disabled', async () => {
    vi.mocked(awsControlApi.accounts).mockRejectedValue(new AwsControlError('app_disabled', 403))
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('aws-control-disabled')).toBeTruthy()
    expect(screen.queryByTestId('accounts-list')).toBeNull()
    expect(screen.queryByTestId('accounts-error')).toBeNull()
  })

  it('renders an error state with retry on a non-403 failure', async () => {
    vi.mocked(awsControlApi.accounts).mockRejectedValue(new AwsControlError('http_500', 500))
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('aws-control-error')).toBeTruthy()
    expect(screen.getByTestId('error-retry')).toBeTruthy()
  })

  it('Add accounts lists only the UNREGISTERED profiles', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    // The section is collapsed by default (the account list stays primary), so
    // open it before the picker renders.
    fireEvent.click(await screen.findByTestId('add-accounts-toggle'))
    const boxes = await screen.findAllByTestId('add-accounts-checkbox')
    const names = boxes.map((b) => b.getAttribute('data-name'))
    // Only staging + sandbox: the already-registered "personal" is not offered.
    expect(names).toEqual(['staging', 'sandbox'])
    expect(names).not.toContain('personal')
  })

  it('register posts exactly the checked names', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    vi.mocked(awsControlApi.registerProfiles).mockResolvedValue({ added: 1, skipped: 0 })
    renderWithProviders(<AwsControlPage />)

    fireEvent.click(await screen.findByTestId('add-accounts-toggle'))
    const boxes = await screen.findAllByTestId('add-accounts-checkbox')
    const staging = boxes.find((b) => b.getAttribute('data-name') === 'staging')!
    fireEvent.click(staging)
    fireEvent.click(screen.getByTestId('add-accounts-register'))

    await waitFor(() => {
      expect(awsControlApi.registerProfiles).toHaveBeenCalledWith(['staging'])
    })
  })

  it('a successful register refetches the account list', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    vi.mocked(awsControlApi.registerProfiles).mockResolvedValue({ added: 1, skipped: 0 })
    renderWithProviders(<AwsControlPage />)

    await screen.findByTestId('accounts-list')
    // One accounts() call for the initial mount before we register.
    expect(awsControlApi.accounts).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByTestId('add-accounts-toggle'))
    const boxes = await screen.findAllByTestId('add-accounts-checkbox')
    fireEvent.click(boxes[0])
    fireEvent.click(screen.getByTestId('add-accounts-register'))

    // Invalidating the ['aws-control','accounts'] key must trigger a refetch so
    // the newly registered profile appears without a manual Refresh.
    await waitFor(() => {
      expect(awsControlApi.accounts).toHaveBeenCalledTimes(2)
    })
  })

  it('says so on an unsupported platform instead of showing a picker', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    vi.mocked(awsControlApi.availableProfiles).mockResolvedValue(
      availablePayload({ profiles: [], supported: false, registeredCount: 0 }),
    )
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('add-accounts-unsupported')).toBeTruthy()
    // No picker, no toggle — an empty list here means "can't tell", not "none".
    expect(screen.queryByTestId('add-accounts-toggle')).toBeNull()
    expect(screen.queryByTestId('add-accounts-checkbox')).toBeNull()
  })

  it('surfaces a message when register fails, never silently', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
    vi.mocked(awsControlApi.registerProfiles).mockRejectedValue(
      new AwsControlError('unknown_profile', 400),
    )
    renderWithProviders(<AwsControlPage />)

    fireEvent.click(await screen.findByTestId('add-accounts-toggle'))
    const boxes = await screen.findAllByTestId('add-accounts-checkbox')
    fireEvent.click(boxes[0])
    fireEvent.click(screen.getByTestId('add-accounts-register'))

    expect(await screen.findByTestId('add-accounts-error')).toBeTruthy()
  })
})
