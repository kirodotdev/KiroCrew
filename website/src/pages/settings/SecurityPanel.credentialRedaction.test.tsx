/**
 * Credential redaction toggle (Settings -> Security -> Redaction).
 *
 * The section is a VIEW over the one setting the `/api/security/credential-redaction`
 * endpoints own, so what is pinned here is what a UI can get wrong in a way that
 * MISLEADS an owner about a security control, not the backend's rules (those are
 * in test/test_redaction_one_setting.py):
 *
 *  - The recorded position is what renders, and flipping it sends exactly that
 *    boolean to the writer.
 *  - It is ONE labelled switch: no engine selector, no second switch, no notice.
 *  - A policy that keeps redaction on LOCKS the switch, with one line saying why.
 *  - A FAILED READ renders NO switch. Showing a default for a state we could not
 *    read is the reassuring direction of wrong, and the dangerous one.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { CredentialRedactionState } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { purgeDocumentBodiesForRedactionChange } from '../../hooks/usePanelTabs'

vi.mock('../../hooks/usePanelTabs', async () => ({
  ...(await vi.importActual<typeof import('../../hooks/usePanelTabs')>('../../hooks/usePanelTabs')),
  evictDocumentBodies: vi.fn(() => 0),
  purgeDocumentBodiesForRedactionChange: vi.fn((qc: { resetQueries: (f: { queryKey: unknown[] }) => unknown }) => { void qc.resetQueries({ queryKey: ['file-read'] }); void qc.resetQueries({ queryKey: ['file-diff'] }) }),
}))

vi.mock('../../api/client', async () => ({
  // The real class: the card's retry policy tells a 403 apart with `instanceof`.
  ApiError: (await vi.importActual<typeof import('../../api/apiError')>('../../api/apiError')).ApiError,
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
    credentialRedaction: vi.fn(),
    setCredentialRedaction: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

const ON: CredentialRedactionState = { enabled: true, changed_at: '', locked_by_policy: false }
const OFF: CredentialRedactionState = { enabled: false, changed_at: '2026-09-24T23:00:00+00:00', locked_by_policy: false }
const LOCKED: CredentialRedactionState = { enabled: true, changed_at: '', locked_by_policy: true }
const LOCKED_LINE = 'An administrator controls this setting.'
async function renderRedaction(state: CredentialRedactionState) {
  ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(state)
  const utils = renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
  await screen.findByRole('switch', { name: LABEL })
  return utils
}

const LABEL = 'Redact credentials'

/** The one switch of the section, found by its accessible name. */
function toggle(): HTMLElement {
  return screen.getByRole('switch', { name: LABEL })
}

describe('SecurityPanel - credential redaction toggle', () => {
  beforeEach(() => {
    cleanup()
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
  })

  it('renders the recorded position: ON', async () => {
    await renderRedaction(ON)
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
  })

  it('renders the recorded position: OFF, which is the default of a fresh install', async () => {
    await renderRedaction(OFF)
    expect(toggle()).toHaveAttribute('aria-checked', 'false')
  })

  it('is one labelled switch and nothing else: no engine selector, no second switch, no notice', async () => {
    await renderRedaction(OFF)
    expect(screen.getAllByRole('switch')).toHaveLength(1)
    expect(toggle()).toHaveAccessibleName(LABEL)
    for (const gone of [/keep ids readable/i, /AI model and server logs/i, /off since/i, /unredacted/i]) {
      expect(screen.queryByText(gone)).toBeNull()
    }
    expect(screen.queryByTestId('credential-redaction-off-notice')).toBeNull()
    expect(screen.queryByTestId('credential-redaction-seams-row')).toBeNull()
    expect(screen.queryByTestId('credential-redaction-engine-row')).toBeNull()
    expect(screen.queryByTestId('credential-redaction-locked')).toBeNull()
  })

  it('has a keyboard-reachable switch whose one sentence sits behind the info tip, as on Settings > Browser', async () => {
    await renderRedaction(OFF)
    expect(toggle().tabIndex).toBeGreaterThanOrEqual(0)
    // No line of prose under the label: the sentence is the info tip's text.
    expect(screen.queryByText(/keys, tokens and passwords/i)).toBeNull()
    expect(screen.getByTitle(/^Hides keys, tokens and passwords in chat, messaging channels/)).toBeTruthy()
  })

  it('says the edition keeps redaction on when no policy can lift the lock', async () => {
    await renderRedaction({ ...LOCKED, locked_by_edition: true })
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByTestId('credential-redaction-locked').textContent).toBe('This edition always redacts credentials, and an admin cannot turn it off.')
  })

  it('is locked, with one line saying an administrator controls it, when policy keeps redaction on', async () => {
    await renderRedaction(LOCKED)
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
    // Out of the tab order, and the switch's description is the locked line.
    expect(toggle().tabIndex).toBe(-1)
    const line = screen.getByTestId('credential-redaction-locked')
    expect(line.textContent).toBe(LOCKED_LINE)
    expect(toggle().getAttribute('aria-describedby')).toBe(line.id)
    // Neither the switch nor its row writes anything.
    fireEvent.click(toggle())
    fireEvent.click(screen.getByText(LABEL))
    fireEvent.keyDown(toggle(), { key: ' ' })
    expect(api.setCredentialRedaction).not.toHaveBeenCalled()
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
  })

  it('is not locked when policy allows it', async () => {
    await renderRedaction(ON)
    expect(toggle().hasAttribute('aria-disabled')).toBe(false)
    expect(toggle().hasAttribute('aria-describedby')).toBe(false)
    expect(screen.queryByTestId('credential-redaction-locked')).toBeNull()
  })

  it('a write refused by policy (403 credential_redaction_locked_by_policy) shows the locked line, not a save failure', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new ApiError(403, 'locked by policy', JSON.stringify({ code: 'credential_redaction_locked_by_policy' })))
    await renderRedaction(ON)
    // The read did not know yet; the re-read on settlement does.
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(LOCKED)
    fireEvent.click(toggle())
    await waitFor(() => expect(api.setCredentialRedaction).toHaveBeenCalledWith(false))
    expect((await screen.findByTestId('credential-redaction-locked')).textContent).toBe(LOCKED_LINE)
    expect(screen.queryByTestId('credential-redaction-write-failed')).toBeNull()
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-disabled', 'true'))
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
    expect(screen.getAllByTestId('credential-redaction-locked')).toHaveLength(1)
  })

  it('switching on sends exactly {enabled: true}', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    await renderRedaction(OFF)
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    fireEvent.click(toggle())
    await waitFor(() => expect(api.setCredentialRedaction).toHaveBeenCalledWith(true))
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'true'))
  })

  it('switching off sends exactly {enabled: false}', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(OFF)
    await renderRedaction(ON)
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(OFF)
    fireEvent.click(toggle())
    await waitFor(() => expect(api.setCredentialRedaction).toHaveBeenCalledWith(false))
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'false'))
  })

  it('a non-owner sees the owner-only notice, read once: no Retry, no hand-off, one audited refusal', async () => {
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new ApiError(403, 'dashboard owner required', JSON.stringify({ code: 'dashboard_owner_required' })))
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('credential-redaction-owner-only')
    expect(api.credentialRedaction).toHaveBeenCalledTimes(1)
    // A fact about the viewer, not a failure: no Retry, no switch -- but the
    // hand-off every ErrorNotice carries is there, and says what it sends.
    expect(screen.queryByTestId('credential-redaction-retry')).toBeNull()
    expect(screen.queryByTestId('credential-redaction-read-failed')).toBeNull()
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.getByText('Open a chat with the agent about this error')).toBeTruthy()
  })

  it('a stale pre-owner session (401 stale_session_reauth) is the same refusal: owner-only notice, read once', async () => {
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new ApiError(401, 'sign in again', JSON.stringify({ code: 'stale_session_reauth' })))
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('credential-redaction-owner-only')
    expect(api.credentialRedaction).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('credential-redaction-retry')).toBeNull()
  })

  it('a re-auth 403 (X-Auth-Required, a lapsed cookie) is NOT the owner gate: the generic failed read with Retry', async () => {
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new ApiError(403, 'sign in', '', true))
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('credential-redaction-read-failed')
    expect(screen.queryByTestId('credential-redaction-owner-only')).toBeNull()
    expect(screen.getByTestId('credential-redaction-retry')).toBeTruthy()
  })

  it('while the first read is pending the card shows a toggle skeleton, not an empty section', async () => {
    let resolve: (v: CredentialRedactionState) => void = () => {}
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockReturnValue(new Promise<CredentialRedactionState>(r => { resolve = r }))
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('credential-redaction-loading')
    expect(screen.queryByRole('switch')).toBeNull()
    resolve(ON)
    await screen.findByRole('switch', { name: LABEL })
    expect(screen.queryByTestId('credential-redaction-loading')).toBeNull()
  })

  it('a failed read renders no switch and says the read failed', async () => {
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('credential-redaction-read-failed')
    expect(screen.queryByRole('switch')).toBeNull()
    // The hand-off says what it does, not the shared bare "Ask the agent".
    expect(screen.getByText('Open a chat with the agent about this error')).toBeTruthy()
  })

  it('Retry on the failed read re-reads the switch without a page reload', async () => {
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('credential-redaction-read-failed')
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    fireEvent.click(screen.getByTestId('credential-redaction-retry'))
    await screen.findByRole('switch', { name: LABEL })
    expect(toggle().getAttribute('aria-checked')).toBe('true')
    expect(screen.queryByTestId('credential-redaction-read-failed')).toBeNull()
  })

  it('flipping the switch drops every cached file body, so a raw read cannot outlive the switch', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    const { queryClient } = await renderRedaction(OFF)
    queryClient.setQueryData(['file-read', '/tmp/raw.txt'], { content: 'AKIA-raw-while-off' })
    queryClient.setQueryData(['file-diff', '/tmp/raw.txt'], { diff: '', original: 'AKIA-raw-while-off', status: 'clean' })
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    fireEvent.click(toggle())
    await waitFor(() => expect(api.setCredentialRedaction).toHaveBeenCalledWith(true))
    await waitFor(() => expect(queryClient.getQueryData(['file-read', '/tmp/raw.txt'])).toBeUndefined())
    expect(queryClient.getQueryData(['file-diff', '/tmp/raw.txt'])).toBeUndefined()
    expect(purgeDocumentBodiesForRedactionChange).toHaveBeenCalledTimes(1)
  })

  it('a REFUSED write purges nothing: the server moved nothing', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new ApiError(503, 'audit unavailable', JSON.stringify({ code: 'credential_redaction_audit_unavailable' })))
    const { queryClient } = await renderRedaction(ON)
    queryClient.setQueryData(['file-read', '/tmp/a.txt'], { content: 'body' })
    fireEvent.click(toggle())
    await screen.findByTestId('credential-redaction-write-failed')
    expect(queryClient.getQueryData(['file-read', '/tmp/a.txt'])).toEqual({ content: 'body' })
    expect(purgeDocumentBodiesForRedactionChange).not.toHaveBeenCalled()
  })

  it('a write whose response is lost still converges on the authoritative read', async () => {
    // The PUT landed server-side but the response never arrived: the card must
    // not keep showing ON while OFF is in force.
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network'))
    await renderRedaction(ON)
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(OFF)
    fireEvent.click(toggle())
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'false'))
  })

  it('a failed write keeps the position in force and says so', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('403'))
    await renderRedaction(ON)
    fireEvent.click(toggle())
    await waitFor(() => expect(api.setCredentialRedaction).toHaveBeenCalledWith(false))
    await screen.findByTestId('credential-redaction-write-failed')
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
  })
})
