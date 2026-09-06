/**
 * Flagged-file delivery consent section (Settings -> Security).
 *
 * What these pin is deliberately narrow: this card is a VIEW over the grant the
 * `/api/file-delivery/consent` endpoints own, so the properties worth locking in
 * are the ones a UI can get wrong in a way that MISLEADS an owner about an
 * authorization, not the backend's rules (those are covered in
 * test/test_file_delivery_consent.py).
 *
 * The security-critical UI property, added with issue #7770's step-up: clicking
 * Allow delivery must NOT record a grant. It ARMS a request, and the card then
 * shows the host command that finishes it. A card that treated the click as the
 * grant would re-open the "an agent-driven browser self-grants" hole the backend
 * split exists to close, so the arm-not-grant behaviour is pinned here.
 *
 * Three further cases exist for a specific hazard:
 *
 *  - A FAILED READ must not render as "Not allowed". Those two states are
 *    opposite in meaning and identical in appearance if you render the fallback,
 *    and the reassuring direction is the dangerous one.
 *  - A class the backend did NOT name as grantable gets NO control.
 *  - PER-ROW pairing with three classes, located by class-keyed testid.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within, cleanup } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { ArmedFileDeliveryConsent, FileDeliveryConsentStatus } from '../../api/client'

vi.mock('../../api/client', () => ({
  api: {
    // The panel's rail reads these on mount regardless of the selected section,
    // and they must RESOLVE: a bare vi.fn() returns undefined, which react-query
    // rejects with "Query data cannot be undefined".
    deniedCommands: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    tailnetStatus: vi.fn(),
    fileDeliveryConsent: vi.fn(),
    fileDeliveryConsentArmStatus: vi.fn(),
    armFileDeliveryConsent: vi.fn(),
    revokeFileDeliveryConsent: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

const OWNER = 'owner_dashboard'
const OWNER_LABEL = 'This computer and your dashboard Files view'
const GRANTED_AT = '2026-09-05T23:00:06+00:00'
const NOT_ARMED: ArmedFileDeliveryConsent = { ok: true, armed: false }

function consent(overrides: Partial<FileDeliveryConsentStatus> = {}): FileDeliveryConsentStatus {
  return {
    ok: true,
    grantable: [OWNER],
    never_grantable: ['channel_upload', 'slack_upload'],
    labels: { [OWNER]: OWNER_LABEL },
    grants: { [OWNER]: null },
    ...overrides,
  }
}

/** Render the panel on the delivery section with the consent query pre-resolved.
 *
 *  Waits on the ROW rather than the card title: the title is present while the
 *  query is still in flight and in the failed-read branch, so waiting on it hands
 *  back a card that has not received `grants` yet. */
async function renderDelivery(data: FileDeliveryConsentStatus) {
  ;(api.fileDeliveryConsent as ReturnType<typeof vi.fn>).mockResolvedValue(data)
  const utils = renderWithProviders(<SecurityPanel />, { route: '/?section=delivery' })
  await screen.findByTestId(`file-delivery-row-${data.grantable[0]}`)
  return utils
}

describe('SecurityPanel - flagged-file delivery consent', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
      builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
    })
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue({ controls: [], counts: {} })
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue({
      version: null, has_policy: false, profile: null, unavailable: false, scopes: [],
    })
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.patchConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off',
    })
    // Default: nothing armed. Individual tests override to the armed view.
    ;(api.fileDeliveryConsentArmStatus as ReturnType<typeof vi.fn>).mockResolvedValue(NOT_ARMED)
    ;(api.armFileDeliveryConsent as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, armed: true, request_id: 'req-1', destination_class: OWNER,
      expires_in: 600, approve_command: 'kirocrew file-delivery approve',
    })
    ;(api.revokeFileDeliveryConsent as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, removed: true })
  })

  it('not allowed: shows the server label, the plain destination helper and an Allow control', async () => {
    await renderDelivery(consent())

    const row = screen.getByTestId(`file-delivery-row-${OWNER}`)
    expect(within(row).getByText(OWNER_LABEL)).toBeInTheDocument()
    expect(within(row).getByText('Not allowed')).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Allow delivery' })).toBeInTheDocument()
    // The plain-language destination helper is present at the point of consent.
    expect(within(row).getByText(/outbox folder on this computer/i)).toBeInTheDocument()
    // Complement: the allowed state appears NOWHERE, so a stray "Allowed"
    // elsewhere in the card cannot make this read as granted.
    expect(within(row).queryByText('Allowed')).not.toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument()
  })

  it('clicking Allow ARMS a step-up (does not record a grant) and shows the host command', async () => {
    // The security-critical property: the click must arm, never grant. Start
    // not-armed so the Allow control is clickable, then the arm-status flips to
    // armed and the card renders the approve command + the armed-state UX.
    ;(api.fileDeliveryConsentArmStatus as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(NOT_ARMED)
      .mockResolvedValue({
        ok: true, armed: true, request_id: 'req-1', destination_class: OWNER,
        expires_in: 600, approve_command: 'kirocrew file-delivery approve',
      })
    await renderDelivery(consent())

    fireEvent.click(within(screen.getByTestId(`file-delivery-row-${OWNER}`)).getByRole('button', { name: 'Allow delivery' }))

    await waitFor(() => expect(api.armFileDeliveryConsent).toHaveBeenCalledWith(OWNER))
    // The armed step-up panel shows the exact host command. This is what proves
    // the click did not itself record the grant.
    const armed = await screen.findByTestId(`file-delivery-armed-${OWNER}`)
    expect(within(armed).getByText('kirocrew file-delivery approve')).toBeInTheDocument()
    // Once armed, the primary CTA becomes a disabled "Waiting..." control so a
    // re-click is not an invisible re-arm (UX Watch item), and the badge reads
    // "Waiting for approval" (not "Allowed") because no grant was recorded.
    await waitFor(() =>
      expect(within(screen.getByTestId(`file-delivery-row-${OWNER}`)).getByRole('button', { name: 'Waiting on this machine' })).toBeDisabled(),
    )
    expect(within(screen.getByTestId(`file-delivery-row-${OWNER}`)).getByText('Waiting for approval')).toBeInTheDocument()
    expect(api.revokeFileDeliveryConsent).not.toHaveBeenCalled()
  })

  it('offers a Copy button on the armed host command', async () => {
    // UX Watch item: the host command must be copyable, not hand-selection only
    // (a mistype inside the 10-minute window silently fails the step-up).
    ;(api.fileDeliveryConsentArmStatus as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(NOT_ARMED)
      .mockResolvedValue({
        ok: true, armed: true, request_id: 'req-1', destination_class: OWNER,
        expires_in: 600, approve_command: 'kirocrew file-delivery approve',
      })
    await renderDelivery(consent())
    fireEvent.click(within(screen.getByTestId(`file-delivery-row-${OWNER}`)).getByRole('button', { name: 'Allow delivery' }))

    const armed = await screen.findByTestId(`file-delivery-armed-${OWNER}`)
    const copyBtn = within(armed).getByRole('button', { name: 'Copy the approval command' })
    expect(copyBtn).toBeInTheDocument()
    fireEvent.click(copyBtn)
    // The click routes through the shared clipboard helper; the label flips to
    // the "Copied" acknowledgement on success.
    await waitFor(() => expect(within(armed).getByText('Copied')).toBeInTheDocument())
  })

  it('re-reads from the server after arming instead of trusting its own cache', async () => {
    await renderDelivery(consent())
    const callsBefore = (api.fileDeliveryConsent as ReturnType<typeof vi.fn>).mock.calls.length

    fireEvent.click(within(screen.getByTestId(`file-delivery-row-${OWNER}`)).getByRole('button', { name: 'Allow delivery' }))

    await waitFor(() => expect(api.armFileDeliveryConsent).toHaveBeenCalledWith(OWNER))
    // The authority is the server, not this cache: an external app can rewrite any
    // react-query key, so the card must refetch rather than assert the new state
    // locally. A cache patch would satisfy the assertion above and skip this one.
    await waitFor(() =>
      expect((api.fileDeliveryConsent as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(callsBefore),
    )
  })

  it('allowed: shows the grant time and a Withdraw control that revokes that class', async () => {
    await renderDelivery(consent({ grants: { [OWNER]: { destination_class: OWNER, granted_at: GRANTED_AT } } }))

    const row = screen.getByTestId(`file-delivery-row-${OWNER}`)
    expect(within(row).getByText('Allowed')).toBeInTheDocument()
    // The timestamp is rendered through the locale-aware formatter, so assert the
    // YEAR is present rather than a hardcoded format string.
    expect(within(row).getByText(/Since .*2026/)).toBeInTheDocument()

    fireEvent.click(within(row).getByRole('button', { name: 'Withdraw' }))
    await waitFor(() => expect(api.revokeFileDeliveryConsent).toHaveBeenCalledWith(OWNER))
    expect(api.armFileDeliveryConsent).not.toHaveBeenCalled()
  })

  it('states the never-grantable legs in plain words rather than raw ids, and offers no control for one', async () => {
    await renderDelivery(consent())

    // The excluded legs are named in plain language (not the raw ids
    // "slack_upload"/"channel_upload" the UX review flagged).
    expect(screen.getByText(/Uploads to Slack or a chat channel can never be allowed/i)).toBeInTheDocument()
    expect(screen.queryByText(/slack_upload/)).not.toBeInTheDocument()
    expect(screen.queryByText(/channel_upload/)).not.toBeInTheDocument()
    // ...and NOT actionable: the card renders rows from `grantable` only, so a
    // class the handler would refuse has no row and therefore no button.
    expect(screen.queryByTestId('file-delivery-row-slack_upload')).not.toBeInTheDocument()
    expect(screen.queryByTestId('file-delivery-row-channel_upload')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Allow delivery' })).toHaveLength(1)
  })

  it('a FAILED read reports the failure and never renders as "Not allowed"', async () => {
    ;(api.fileDeliveryConsent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=delivery' })

    expect(await screen.findByText('Could not read which destinations are allowed.')).toBeInTheDocument()
    // The whole point: an unreadable authorization state must not be shown as an
    // absent one. Both the row and the reassuring label are absent.
    expect(screen.queryByText('Not allowed')).not.toBeInTheDocument()
    expect(screen.queryByTestId(`file-delivery-row-${OWNER}`)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Allow delivery' })).not.toBeInTheDocument()
  })

  it('a failed RE-READ after a successful arm does not keep rendering the stale state', async () => {
    // react-query keeps the last good `data` when a refetch rejects, so a card
    // that renders rows whenever `data` is present would go on showing the
    // pre-arm state for a grant whose state is now unknown. `view` is gated on
    // `isError`, so the failure notice is the only thing that renders.
    await renderDelivery(consent())
    ;(api.fileDeliveryConsent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('refetch down'))

    fireEvent.click(within(screen.getByTestId(`file-delivery-row-${OWNER}`)).getByRole('button', { name: 'Allow delivery' }))
    await waitFor(() => expect(api.armFileDeliveryConsent).toHaveBeenCalledWith(OWNER))

    expect(await screen.findByText('Could not read which destinations are allowed.')).toBeInTheDocument()
    expect(screen.queryByText('Not allowed')).not.toBeInTheDocument()
    expect(screen.queryByTestId(`file-delivery-row-${OWNER}`)).not.toBeInTheDocument()
  })

  it('a failed ARM is reported without claiming the state changed', async () => {
    await renderDelivery(consent())
    ;(api.armFileDeliveryConsent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('nope'))

    fireEvent.click(within(screen.getByTestId(`file-delivery-row-${OWNER}`)).getByRole('button', { name: 'Allow delivery' }))

    expect(await screen.findByText('Could not save that change.')).toBeInTheDocument()
    // The row still reads not allowed, because the server never armed it.
    expect(within(screen.getByTestId(`file-delivery-row-${OWNER}`)).getByText('Not allowed')).toBeInTheDocument()
  })

  it('a failed arm-STATUS read is surfaced, not silently hidden', async () => {
    // GPT F1: a failed arm-status GET must say so rather than dropping the
    // command panel with no explanation. The row still renders (the consent GET
    // succeeded); the arm-status error gets its own notice.
    ;(api.fileDeliveryConsentArmStatus as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('arm boom'))
    await renderDelivery(consent())

    expect(
      await screen.findByText('Could not check whether an approval is in progress.'),
    ).toBeInTheDocument()
    // The row is still present (only the arm-status read failed), so the failure
    // is reported WITHOUT hiding the rest of the card.
    expect(screen.getByTestId(`file-delivery-row-${OWNER}`)).toBeInTheDocument()
  })

  it('pairs each row with ITS OWN grant, not the first one', async () => {
    // Three classes so a loop-variable slip is observable, and two DISTINCT
    // timestamps so a row cannot pass by rendering a sibling's value.
    const second = 'second_class'
    const third = 'third_class'
    const otherAt = '2024-01-02T03:04:05+00:00'
    await renderDelivery(consent({
      grantable: [OWNER, second, third],
      labels: { [OWNER]: OWNER_LABEL, [second]: 'Second destination', [third]: 'Third destination' },
      grants: {
        [OWNER]: { destination_class: OWNER, granted_at: GRANTED_AT },
        [second]: null,
        [third]: { destination_class: third, granted_at: otherAt },
      },
    }))

    const first = screen.getByTestId(`file-delivery-row-${OWNER}`)
    const middle = screen.getByTestId(`file-delivery-row-${second}`)
    const last = screen.getByTestId(`file-delivery-row-${third}`)

    // Per-item pairing: each row's own state AND its own timestamp.
    expect(within(first).getByText('Allowed')).toBeInTheDocument()
    expect(within(first).getByText(/Since .*2026/)).toBeInTheDocument()

    expect(within(middle).getByText('Not allowed')).toBeInTheDocument()
    expect(within(middle).getByRole('button', { name: 'Allow delivery' })).toBeInTheDocument()
    // The unconfirmed row shows NO timestamp at all, so it cannot have inherited
    // a sibling's grant.
    expect(within(middle).queryByText(/Since /)).not.toBeInTheDocument()

    expect(within(last).getByText('Allowed')).toBeInTheDocument()
    expect(within(last).getByText(/Since .*2024/)).toBeInTheDocument()

    // And the two allowed rows carry DIFFERENT years, which a shared read of
    // one grant could not produce.
    expect(within(first).queryByText(/Since .*2024/)).not.toBeInTheDocument()
    expect(within(last).queryByText(/Since .*2026/)).not.toBeInTheDocument()

    // Withdrawing the LAST row must name the last class, not the first.
    fireEvent.click(within(last).getByRole('button', { name: 'Withdraw' }))
    await waitFor(() => expect(api.revokeFileDeliveryConsent).toHaveBeenCalledWith(third))
    expect(api.revokeFileDeliveryConsent).not.toHaveBeenCalledWith(OWNER)
  })

  it('renders the raw class id when the server sent no label for it', async () => {
    cleanup()
    await renderDelivery(consent({ labels: {} }))
    // A labelless class still identifies itself rather than rendering an empty row.
    expect(within(screen.getByTestId(`file-delivery-row-${OWNER}`)).getByText(OWNER)).toBeInTheDocument()
  })

  /* The RAIL row deliberately carries NO summary, and that is a render-gate
   * decision rather than an oversight: `SettingsSubNav` renders a label and a
   * summary as two adjacent catalog keys, which the render-time i18n gate counts
   * as a `fragment/multi-unit` finding, and `[vs-base]` fails on any per-surface
   * increase against a goal of zero. Pinned here so re-adding a summary reddens a
   * fast unit test instead of a five-minute browser gate in CI. */
  it('gives the rail row NO summary, even when a grant is held', async () => {
    await renderDelivery(consent({ grants: { [OWNER]: { destination_class: OWNER, granted_at: GRANTED_AT } } }))

    const row = screen.getAllByRole('option').find(o => o.textContent?.includes('Flagged-file delivery'))
    expect(row).toBeDefined()
    // The card says "Allowed"; the rail must not, or it adds two catalog
    // fragments to a surface the render gate holds at zero.
    expect(row?.textContent).toBe('Flagged-file delivery')
  })
})
