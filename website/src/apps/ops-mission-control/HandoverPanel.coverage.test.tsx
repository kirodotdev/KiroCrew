/**
 * HandoverPanel: the digest body, beyond the copy-as-text control.
 *
 * The digest must show the work a responder inherits: each open incident in its own section with the
 * blocked reason and the by-hand claim marked, a recurring pattern with its proof state (proven, or
 * demoted after the fix kept failing), a watched source whose last poll failed marked as not answering,
 * and a readable error when the digest cannot be built. Refresh re-requests the digest. The API module
 * is mocked at the network boundary; the panel and its helpers are real.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import HandoverPanel from './HandoverPanel'
import { blockedLabel, opsApi, SIGNALS_QUERY_KEY, type HandoverDigest, type HandoverIncident } from './api'

vi.mock('../../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyCode: vi.fn().mockResolvedValue(true),
}))

vi.mock('./api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('./api')>()
  return {
    ...mod,
    opsApi: { ...mod.opsApi, handover: vi.fn(), signals: vi.fn(), providers: vi.fn() },
  }
})

const incident = (id: string, extra: Partial<HandoverIncident> = {}): HandoverIncident =>
  ({ id, title: `incident ${id}`, blocked_reason: '', claimed_by: 'heartbeat', source: 'pager', ...extra }) as HandoverIncident

function digest(overrides: Partial<HandoverDigest> = {}): HandoverDigest {
  return {
    headline: 'Two things need you.',
    text: 'handover text',
    open_work: { waiting_on_you: [], stalled_without_diagnosis: [], escalated: [] },
    recurring_patterns: [],
    coverage: { watching: [], not_configured: [], any_watching: false },
    autonomy: null,
    ...overrides,
  } as unknown as HandoverDigest
}

beforeEach(() => {
  vi.mocked(opsApi.handover).mockResolvedValue(digest())
  vi.mocked(opsApi.providers).mockResolvedValue({ providers: [] } as never)
  vi.mocked(opsApi.signals).mockResolvedValue({} as never)
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('HandoverPanel digest sections', () => {
  it('lists waiting, stalled and escalated incidents, marking a hand claim and a blocked reason', async () => {
    vi.mocked(opsApi.handover).mockResolvedValue(
      digest({
        open_work: {
          waiting_on_you: [incident('INC-1', { claimed_by: 'operator', blocked_reason: 'awaiting_human' as never })],
          stalled_without_diagnosis: [incident('INC-2')],
          escalated: [incident('INC-3')],
        },
      } as never),
    )
    renderWithProviders(<HandoverPanel />)

    expect(await screen.findByText('incident INC-1')).toBeTruthy()
    expect(screen.getByText('incident INC-2')).toBeTruthy()
    expect(screen.getByText('incident INC-3')).toBeTruthy()
    expect(screen.getByText(i18nT('apps.opsMissionControl.handoverPanel.claimed_by_hand'))).toBeTruthy()
    expect(screen.getAllByText(i18nT('apps.opsMissionControl.handoverPanel.claimed_by_hand'))).toHaveLength(1)
    expect(screen.getByText(blockedLabel('awaiting_human'))).toBeTruthy()
  })

  it('separates a proven pattern from a demoted one that kept recurring after its fix', async () => {
    vi.mocked(opsApi.handover).mockResolvedValue(
      digest({
        recurring_patterns: [
          { pattern: 'disk full', uses: 3, proven: true, demoted: false, fix: 'clear tmp', confidence: 'high', trust: 'high' },
          { pattern: 'oom loop', uses: 5, proven: false, demoted: true, misses: 2, fix: 'raise limit', confidence: 'low', trust: 'low' },
        ],
      } as never),
    )
    renderWithProviders(<HandoverPanel />)

    expect(await screen.findByText('disk full')).toBeTruthy()
    expect(screen.getByText('clear tmp')).toBeTruthy()
    expect(screen.getByText(i18nT('apps.opsMissionControl.handoverPanel.pattern_proven'))).toBeTruthy()
    expect(screen.getByText('oom loop')).toBeTruthy()
    expect(screen.getByText('raise limit')).toBeTruthy()
    expect(screen.getByText(/signal was still firing afterwards/)).toBeTruthy()
  })

  it('marks a watched source whose last poll failed as not answering', async () => {
    vi.mocked(opsApi.handover).mockResolvedValue(
      digest({ coverage: { watching: ['Acme', 'Beta'], not_configured: [], any_watching: true } } as never),
    )
    vi.mocked(opsApi.providers).mockResolvedValue({
      providers: [
        { id: 'acme', display_name: 'Acme', roles: ['signal'], configured: true },
        { id: 'beta', display_name: 'Beta', roles: ['signal'], configured: true },
      ],
    } as never)
    const { queryClient } = renderWithProviders(<HandoverPanel />)
    queryClient.setQueryData(SIGNALS_QUERY_KEY, {
      poll_health: {},
      errors: { acme: 'upstream returned 401' },
    })

    const marked = i18nT('apps.opsMissionControl.handoverPanel.source_not_answering', { name: 'Acme' })
    await waitFor(() => expect(document.body.textContent).toContain(marked))
    expect(document.body.textContent).toContain('Beta')
    expect(document.body.textContent).not.toContain(
      i18nT('apps.opsMissionControl.handoverPanel.source_not_answering', { name: 'Beta' }),
    )
  })
})

describe('HandoverPanel error and refresh', () => {
  it('shows the reason when the digest cannot be built', async () => {
    vi.mocked(opsApi.handover).mockRejectedValue(new Error('digest store unreadable'))
    renderWithProviders(<HandoverPanel />)
    expect(await screen.findByText('digest store unreadable')).toBeTruthy()
  })

  it('requests the digest again when Refresh is pressed', async () => {
    renderWithProviders(<HandoverPanel />)
    const refresh = await screen.findByText(i18nT('apps.opsMissionControl.handoverPanel.refresh'))
    await waitFor(() => expect(vi.mocked(opsApi.handover)).toHaveBeenCalled())
    const before = vi.mocked(opsApi.handover).mock.calls.length
    await act(async () => {
      fireEvent.click(refresh)
    })
    await waitFor(() => expect(vi.mocked(opsApi.handover).mock.calls.length).toBeGreaterThan(before))
  })

  it('keeps keyboard focus on Refresh while it runs, and ignores a second press', async () => {
    // A native `disabled` takes the focused button out of the tab order, so a
    // keyboard press dropped focus to the page for the whole refresh.
    renderWithProviders(<HandoverPanel />)
    const label = await screen.findByText(i18nT('apps.opsMissionControl.handoverPanel.refresh'))
    const button = label.closest('button') as HTMLButtonElement
    let release: (v: unknown) => void = () => {}
    vi.mocked(opsApi.handover).mockReturnValue(new Promise(resolve => { release = resolve }) as never)
    const calls = vi.mocked(opsApi.handover).mock.calls.length

    button.focus()
    fireEvent.click(button)
    await screen.findByText(i18nT('apps.opsMissionControl.handoverPanel.refreshing'))
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).not.toBeDisabled()
    expect(button).toHaveFocus()

    fireEvent.click(button)
    expect(vi.mocked(opsApi.handover).mock.calls.length).toBe(calls + 1)

    await act(async () => { release(digest()) })
    await screen.findByText(i18nT('apps.opsMissionControl.handoverPanel.refresh'))
    expect(button).not.toHaveAttribute('aria-disabled')
    expect(button).toHaveFocus()
  })
})
