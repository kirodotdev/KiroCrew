/**
 * OpsMissionControlPage — the closed-postmortem "Copy postmortem" button.
 *
 * Same defect class as HandoverPanel's copy button: it used to early-return
 * when `navigator.clipboard` was falsy and otherwise swallow a rejection, so a
 * blocked clipboard rendered no error AND no confirmation while copying
 * nothing. It now routes through the shared `copyToClipboard` helper and
 * gates "Copied" on the resolved boolean.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import OpsMissionControlPage from './OpsMissionControlPage'
import { opsApi, type BoardState, type Incident } from './api'

vi.mock('../../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyCode: vi.fn().mockResolvedValue(true),
}))

import { copyToClipboard } from '../../utils/clipboard'

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

const CLOSED_INCIDENT = {
  incident_id: 'zzq-1',
  signal: { title: 'Disk nearly full', severity: 'warning', source: 'cloudwatch' },
  status: 'resolved',
  updated_at: '2026-01-01T00:00:00Z',
  claimed_at: '2026-01-01T00:00:00Z',
  ledger_matches: [],
} as unknown as Incident

function boardState(): BoardState {
  return {
    incidents: [],
    counts: {},
    providers: [],
    rotation: { on_shift: false, who: '', until: '', unknown: false, roster: null },
    ledger: { total: 0, proven: 0, demoted: 0 },
    webhook_queue: 0,
  } as unknown as BoardState
}

describe('OpsMissionControlPage closed postmortem copy', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.mocked(copyToClipboard).mockResolvedValue(true)
    vi.mocked(opsApi.state).mockResolvedValue(boardState())
    vi.mocked(opsApi.ledger).mockResolvedValue({ entries: [] })
    vi.mocked(opsApi.incidents).mockResolvedValue({ incidents: [CLOSED_INCIDENT] })
    vi.mocked(opsApi.incident).mockResolvedValue({
      incident: CLOSED_INCIDENT,
      log: 'postmortem log body',
      log_path: '/data/incidents/zzq-1.md',
    })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  async function openPostmortem() {
    renderWithProviders(<OpsMissionControlPage />)
    const row = await screen.findByTestId('omc-closed-row')
    fireEvent.click(row)
    return screen.findByText(i18nT('apps.opsMissionControl.opsMissionControlPage.copy_postmortem'))
  }

  it('copies the postmortem log through the shared helper and confirms, then reverts', async () => {
    const btn = await openPostmortem()
    fireEvent.click(btn)

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith('postmortem log body'))
    await waitFor(() =>
      expect(
        screen.getByText(i18nT('apps.opsMissionControl.opsMissionControlPage.copied')),
      ).toBeInTheDocument(),
    )

    vi.advanceTimersByTime(2100)
    await waitFor(() =>
      expect(
        screen.getByText(i18nT('apps.opsMissionControl.opsMissionControlPage.copy_postmortem')),
      ).toBeInTheDocument(),
    )
  })

  it('renders no confirmation when the copy fails', async () => {
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    const btn = await openPostmortem()
    fireEvent.click(btn)

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalled())
    expect(
      screen.queryByText(i18nT('apps.opsMissionControl.opsMissionControlPage.copied')),
    ).toBeNull()
  })
})
