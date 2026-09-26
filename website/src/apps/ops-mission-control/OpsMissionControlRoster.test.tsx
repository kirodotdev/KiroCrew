/**
 * OpsMissionControlPage — the on-call card fed by either rotation source.
 *
 * The card was written for a committed `rotation.yaml` and now also renders incident.io's
 * roster of this operator's shifts. What is pinned here is what must NOT cross over: an
 * incident.io roster names members by display name (its `login` is an opaque user id), shows
 * its own not-on-roster remedy, and never shows the schedule-file warnings about a GitHub
 * login or strict gating.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import OpsMissionControlPage from './OpsMissionControlPage'
import { opsApi, type BoardState, type RotationRoster } from './api'

vi.mock('./api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('./api')>()
  return {
    ...mod,
    opsApi: {
      ...mod.opsApi,
      state: vi.fn(),
      ledger: vi.fn(),
      signals: vi.fn(),
      incidents: vi.fn(),
      incident: vi.fn(),
    },
  }
})

const HOUR = 3600_000

function iso(offsetHours: number): string {
  return new Date(Date.now() + offsetHours * HOUR).toISOString()
}

function incidentioRoster(overrides: Partial<RotationRoster> = {}): RotationRoster {
  return {
    source: 'incidentio',
    members: [
      { login: 'UALICE', name: 'Alice Example', shifts: 1, on_call_now: true },
      { login: 'UME', name: 'Sam Operator', shifts: 1, on_call_now: false },
    ],
    windows: [
      { from: iso(-2), to: iso(6), who: ['UALICE'], current: true },
      { from: iso(30), to: iso(54), who: ['UME'], current: false },
    ],
    timezone: 'UTC',
    me: 'UME',
    me_on_roster: true,
    strict_gating: false,
    leader: '',
    error: '',
    ...overrides,
  }
}

function boardState(roster: RotationRoster): BoardState {
  return {
    incidents: [],
    counts: {},
    providers: [],
    rotation: { on_shift: false, who: '', until: '', unknown: false, roster },
    ledger: { total: 0, proven: 0, demoted: 0 },
    webhook_queue: 0,
  } as unknown as BoardState
}

describe('OpsMissionControlPage on-call card, incident.io roster', () => {
  beforeEach(() => {
    vi.mocked(opsApi.ledger).mockResolvedValue({ entries: [] })
    vi.mocked(opsApi.incidents).mockResolvedValue({ incidents: [] })
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows members by name and says when this operator is next on', async () => {
    vi.mocked(opsApi.state).mockResolvedValue(boardState(incidentioRoster()))
    renderWithProviders(<OpsMissionControlPage />)

    expect(await screen.findByText('Alice Example')).toBeInTheDocument()
    expect(
      screen.getByText(
        i18nT('apps.opsMissionControl.opsMissionControlPage.member_is_this_instance', {
          login: 'Sam Operator',
        }),
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText('UALICE')).toBeNull()
    expect(
      screen.getByText(i18nT('apps.opsMissionControl.opsMissionControlPage.incidentio_roster_window')),
    ).toBeInTheDocument()
    expect(screen.getByText(/Your next shift:/)).toBeInTheDocument()
  })

  it('names the incident.io remedy, never the GitHub-login or strict-gating ones', async () => {
    vi.mocked(opsApi.state).mockResolvedValue(
      boardState(
        incidentioRoster({
          members: [{ login: 'UALICE', name: 'Alice Example', shifts: 1, on_call_now: true }],
          windows: [{ from: iso(-2), to: iso(6), who: ['UALICE'], current: true }],
          me_on_roster: false,
        }),
      ),
    )
    renderWithProviders(<OpsMissionControlPage />)

    expect(
      await screen.findByText(i18nT('apps.opsMissionControl.opsMissionControlPage.incidentio_not_on_roster')),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Your next shift:/)).toBeNull()
    expect(
      screen.queryByText(
        i18nT('apps.opsMissionControl.opsMissionControlPage.not_on_roster_lenient', { me: 'UME' }),
      ),
    ).toBeNull()
    expect(
      screen.queryByText(
        i18nT('apps.opsMissionControl.opsMissionControlPage.no_github_login_resolved_for_this_instance_so_it'),
      ),
    ).toBeNull()
  })

  it('renders a window with no shifts at all, and says this operator has none', async () => {
    vi.mocked(opsApi.state).mockResolvedValue(
      boardState(incidentioRoster({ members: [], windows: [], me_on_roster: false })),
    )
    renderWithProviders(<OpsMissionControlPage />)

    expect(
      await screen.findByText(i18nT('apps.opsMissionControl.opsMissionControlPage.incidentio_not_on_roster')),
    ).toBeInTheDocument()
  })

  it('still renders the card for the no-schedules error alone, in the active language', async () => {
    vi.mocked(opsApi.state).mockResolvedValue(
      boardState(
        incidentioRoster({
          members: [],
          windows: [],
          me_on_roster: false,
          error: 'an incident.io user id is set but no schedule_ids are configured',
          error_code: 'no_schedule_ids',
        }),
      ),
    )
    renderWithProviders(<OpsMissionControlPage />)

    const translated = i18nT('apps.opsMissionControl.opsMissionControlPage.incidentio_error_no_schedule_ids')
    expect(
      await screen.findByText(
        i18nT('apps.opsMissionControl.opsMissionControlPage.schedule_problem', { error: translated }),
      ),
    ).toBeInTheDocument()
  })

  it('names the HTTP status of a failed read from its code, not the server English', async () => {
    vi.mocked(opsApi.state).mockResolvedValue(
      boardState(
        incidentioRoster({
          members: [],
          windows: [],
          me_on_roster: false,
          error: 'incident.io did not answer (HTTP 401)',
          error_code: 'unreachable',
          error_status: 401,
        }),
      ),
    )
    renderWithProviders(<OpsMissionControlPage />)

    const translated = i18nT('apps.opsMissionControl.opsMissionControlPage.incidentio_error_unreachable', {
      status: 401,
    })
    expect(
      await screen.findByText(
        i18nT('apps.opsMissionControl.opsMissionControlPage.schedule_problem', { error: translated }),
      ),
    ).toBeInTheDocument()
  })
})
