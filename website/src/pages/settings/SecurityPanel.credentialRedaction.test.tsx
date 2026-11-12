/**
 * Credential redaction switch (Settings -> Security).
 *
 * The card is a VIEW over the switch the `/api/security/credential-redaction`
 * endpoints own, so what is pinned here is what a UI can get wrong in a way that
 * MISLEADS an owner about a security control, not the backend's rules (those are
 * in test/test_credential_redaction_switch.py):
 *
 *  - The recorded position is what renders, and flipping it sends exactly that
 *    boolean to the writer.
 *  - OFF is loud: a notice with the time it was switched off, so an owner who
 *    forgot cannot mistake the page for the default.
 *  - A FAILED READ renders NO switch. Showing the default (ON) for a state we
 *    could not read is the reassuring direction of wrong, and the dangerous one.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'

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
    setCredentialRedactionSeams: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

const ON: CredentialRedactionState = { enabled: true, changed_at: '' }
const OFF: CredentialRedactionState = { enabled: false, changed_at: '2026-09-24T23:00:00+00:00' }
const SEAMS_OFF: CredentialRedactionState = {
  enabled: true, changed_at: '', seams_enabled: false, seams_changed_at: '2026-11-11T19:00:00+00:00',
}

async function renderRedaction(state: CredentialRedactionState) {
  ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(state)
  const utils = renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
  await screen.findByTestId('credential-redaction-row')
  return utils
}

/** The file-view switch. The section has a second one, so each is found in its own row. */
function toggle(): HTMLElement {
  // The file-view switch; the prompt-and-logs switch and the engine selector are their own.
  return within(screen.getByTestId('credential-redaction-row')).getByRole('switch')
}

/** The prompt-and-logs switch. */
function seamsToggle(): HTMLElement {
  return within(screen.getByTestId('credential-redaction-seams-row')).getByRole('switch')
}

describe('SecurityPanel - credential redaction switch', () => {
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

  it('renders the recorded position, ON by default, with no off-notice', async () => {
    await renderRedaction(ON)
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByTestId('credential-redaction-off-notice')).toBeNull()
  })

  it('switching off sends exactly {enabled: false} and shows the off-notice with the time', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(OFF)
    await renderRedaction(ON)
    // The card re-reads on settlement, so the read must now report the new state,
    // as the real backend does.
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(OFF)
    fireEvent.click(toggle())
    await waitFor(() => expect(api.setCredentialRedaction).toHaveBeenCalledWith(false))
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'false'))
    const notice = await screen.findByTestId('credential-redaction-off-notice')
    expect(notice.textContent).toMatch(/off/i)
    expect(notice.textContent).toMatch(/2026/)
  })

  it('switching back on sends exactly {enabled: true} and clears the notice', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    await renderRedaction(OFF)
    expect(screen.getByTestId('credential-redaction-off-notice')).toBeTruthy()
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    fireEvent.click(toggle())
    await waitFor(() => expect(api.setCredentialRedaction).toHaveBeenCalledWith(true))
    await waitFor(() => expect(screen.queryByTestId('credential-redaction-off-notice')).toBeNull())
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
    // The recipe names 'the switch below'; a refused viewer has none, so it is hidden.
    expect(screen.queryByText('Turn off the switch below.')).toBeNull()
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
    await screen.findByTestId('credential-redaction-row')
    expect(screen.queryByTestId('credential-redaction-loading')).toBeNull()
  })

  it('a failed read renders no switch and says the read failed', async () => {
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('credential-redaction-read-failed')
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByTestId('credential-redaction-row')).toBeNull()
    // The hand-off says what it does, not the shared bare "Ask the agent".
    expect(screen.getByText('Open a chat with the agent about this error')).toBeTruthy()
  })

  it('Retry on the failed read re-reads the switch without a page reload', async () => {
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('credential-redaction-read-failed')
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    fireEvent.click(screen.getByTestId('credential-redaction-retry'))
    await screen.findByTestId('credential-redaction-row')
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
    await screen.findByTestId('credential-redaction-off-notice')
  })

  it('a failed write keeps the position in force and says so', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('403'))
    await renderRedaction(ON)
    fireEvent.click(toggle())
    await waitFor(() => expect(api.setCredentialRedaction).toHaveBeenCalledWith(false))
    await screen.findByTestId('credential-redaction-write-failed')
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByTestId('credential-redaction-off-notice')).toBeNull()
  })
})

describe('SecurityPanel - prompt-and-logs redaction switch', () => {
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

  it('is ON by default and says in one line what turning it off does', async () => {
    await renderRedaction(ON)
    await screen.findByTestId('credential-redaction-seams-row')
    expect(seamsToggle()).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByTestId('credential-redaction-seams-off-notice')).toBeNull()
    expect(screen.getByText('AI model and server logs')).toBeInTheDocument()
    const row = screen.getByTestId('credential-redaction-seams-row').textContent ?? ''
    expect(row).toMatch(/Hide credentials from the AI model and server logs/)
    expect(row).toMatch(/Chats, Slack and values on the Secrets page stay protected either way/)
    // The card carries no mechanism wording: the old scope list is gone.
    expect(screen.queryByTestId('credential-redaction-seams-scope')).toBeNull()
    for (const term of [/structured detectors/i, /pattern passes/i, /credential-shaped/i, /seams?\b/i]) {
      expect(row).not.toMatch(term)
    }
  })

  it('an old file-view OFF record keeps its meaning: the file switch is off, this one is on', async () => {
    await renderRedaction(OFF)
    await screen.findByTestId('credential-redaction-seams-row')
    expect(toggle()).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByTestId('credential-redaction-off-notice')).toBeTruthy()
    expect(seamsToggle()).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByTestId('credential-redaction-seams-off-notice')).toBeNull()
  })

  it('switching it off sends only the seams switch, purges no file body and shows its own notice', async () => {
    ;(api.setCredentialRedactionSeams as ReturnType<typeof vi.fn>).mockResolvedValue(SEAMS_OFF)
    await renderRedaction(ON)
    await screen.findByTestId('credential-redaction-seams-row')
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(SEAMS_OFF)
    fireEvent.click(seamsToggle())
    await waitFor(() => expect(api.setCredentialRedactionSeams).toHaveBeenCalledWith(false))
    expect(api.setCredentialRedaction).not.toHaveBeenCalled()
    const notice = await screen.findByTestId('credential-redaction-seams-off-notice')
    expect(notice.textContent).toMatch(/2026/)
    expect(seamsToggle()).toHaveAttribute('aria-checked', 'false')
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
    expect(purgeDocumentBodiesForRedactionChange).not.toHaveBeenCalled()
  })

  it('switching it back on sends exactly {seams_enabled: true} and clears its notice', async () => {
    ;(api.setCredentialRedactionSeams as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    await renderRedaction(SEAMS_OFF)
    await screen.findByTestId('credential-redaction-seams-off-notice')
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    fireEvent.click(seamsToggle())
    await waitFor(() => expect(api.setCredentialRedactionSeams).toHaveBeenCalledWith(true))
    await waitFor(() => expect(screen.queryByTestId('credential-redaction-seams-off-notice')).toBeNull())
  })

  it('a failed write keeps the position in force and says so', async () => {
    ;(api.setCredentialRedactionSeams as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('403'))
    await renderRedaction(ON)
    await screen.findByTestId('credential-redaction-seams-row')
    fireEvent.click(seamsToggle())
    const failed = await screen.findByTestId('credential-redaction-seams-write-failed')
    // Names this switch: the page has three redaction switches, and a bare
    // "Redaction is still on." left a blind reader unable to tell which one failed.
    expect(failed.textContent).toContain('Credentials are still hidden from the AI model and server logs.')
    expect(seamsToggle()).toHaveAttribute('aria-checked', 'true')
  })

  it('a failed read renders neither switch, and the error is shown once', async () => {
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction', queryDefaults: { retryDelay: 0 } })
    await screen.findByTestId('credential-redaction-read-failed')
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByTestId('credential-redaction-seams-row')).toBeNull()
    expect(screen.getAllByText('Open a chat with the agent about this error')).toHaveLength(1)
  })
})
