/**
 * Coverage pass over Settings ▸ Slack (`pages/settings/SlackPanel.tsx`).
 *
 * Nothing mounted this panel before: `ChannelsPanel.test.tsx` and
 * `SettingsPage.test.tsx` only assert that the nav routes to it. So every branch
 * in the file was unexecuted — the three status pills, the four connection
 * hints, the manifest card's loaded and failed shapes, the read-only remote
 * view, the whole `TagListEditor` (add / reject / dedupe / remove), the save
 * payload's folder and credential folding, and both mutation arms.
 *
 * Harness notes:
 *  - `api` is a plain object literal, so `vi.spyOn` on the three Slack methods
 *    is enough; the module stays real and nothing else in the tree is stubbed.
 *  - `renderWithProviders` supplies the QueryClient with `retry: false`, so a
 *    rejected query or mutation surfaces on the first attempt.
 *  - Credentials in this file are obvious placeholders. The panel only ever
 *    forwards them to a mocked writer, and `SecretField` never renders a stored
 *    secret back, so there is nothing real to put here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import { api, type SlackConfigData } from '../api/client'
import { ApiError } from '../api/apiError'
import { SlackPanel } from '../pages/settings/SlackPanel'

/* ── timers ───────────────────────────────────────────────────────────────── */

// The panel schedules three deferred resets (copied pill 1.5s, saved pill 6s,
// save error 8s). On the real clock those callbacks fire after vitest tears the
// environment down and throw "window is not defined" as an UNHANDLED error —
// every test passing and the run still exiting non-zero. Fake timers keep them
// off the wall clock and `clearAllTimers` drops the pending ones at teardown;
// `shouldAdvanceTime` keeps the clock moving so `findBy*` behaves as it does
// with real timers.
/** What GET /api/config/schema answers: slack.command is the one boot-only field. */
const SCHEMA_ENTRIES = [
  { path: 'slack.command', type: 'string', requiresRestart: true },
  { path: 'slack.show_thinking', type: 'boolean' },
]

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  // The panel's restart badge fetches the schema; without a server that fetch
  // fails and the badge honestly renders an error notice (role="alert"), which
  // would collide with the save-error assertions below.
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    json: async () => ({ entries: SCHEMA_ENTRIES }),
  })) as unknown as typeof fetch)
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

/* ── fixtures ─────────────────────────────────────────────────────────────── */

type SaveResult = Awaited<ReturnType<typeof api.saveSlackConfig>>

const MANIFEST = {
  alias: 'tester',
  manifest: 'display_information:\n  name: KiroCrew-tester\n',  // brand-ok: product emits KiroCrew-<alias> (slack-manifest.yaml)
  create_url: 'https://example.invalid/apps/new',
}

/** Not-connected, fully configured, nothing stored — the most branch-rich start. */
function config(over: Partial<SlackConfigData> = {}): SlackConfigData {
  return {
    connected: false,
    connect_error: '',
    configured: true,
    read_only: false,
    bot_token_set: false,
    app_token_set: false,
    bot_token_preview: '',
    app_token_preview: '',
    owner_id: 'U000OWNER',
    command: 'kirocrew',
    allowed_enterprise_ids: ['E000ONE'],
    reactions_enabled: false,
    show_thinking: false,
    ...over,
  }
}

const OK: SaveResult = { ok: true, restart_required: false, reconnect_required: false, verify_warning: '' }

interface SeedOpts {
  /** Fail the manifest query instead of resolving it. */
  manifestFails?: boolean
  /** Result of a save, or a rejection to drive the `onError` arm. */
  save?: SaveResult | { reject: unknown } | { pending: true }
}

/** Install the three Slack API seams and mount the panel. */
function seed(cfgOver: Partial<SlackConfigData> = {}, opts: SeedOpts = {}) {
  vi.spyOn(api, 'getSlackConfig').mockResolvedValue(config(cfgOver))

  const manifest = vi.spyOn(api, 'getSlackManifest')
  if (opts.manifestFails) manifest.mockRejectedValue(new Error('manifest unavailable'))
  else manifest.mockResolvedValue(MANIFEST)

  const save = vi.spyOn(api, 'saveSlackConfig')
  let settle: (v: SaveResult) => void = () => {}
  const want = opts.save ?? OK
  if (want && typeof want === 'object' && 'reject' in want) {
    save.mockRejectedValue(want.reject)
  } else if (want && typeof want === 'object' && 'pending' in want) {
    save.mockReturnValue(new Promise<SaveResult>(r => { settle = r }))
  } else {
    save.mockResolvedValue(want as SaveResult)
  }

  const view = renderWithProviders(<SlackPanel />)
  return { ...view, save, settle }
}

/** Resolve once the query has hydrated and the form is on screen. */
async function hydrated() {
  return await screen.findByRole('heading', { name: 'Slack', level: 3 })
}

/** The save button, which only exists on a writable session. */
function saveBtn() {
  return screen.getByRole('button', { name: 'Save Slack settings' })
}

/** Install a clipboard whose `writeText` is observable (happy-dom's is getter-only). */
function stubClipboard(impl: () => Promise<void>) {
  const writeText = vi.fn(impl)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  return writeText
}

/* ── query states ─────────────────────────────────────────────────────────── */

describe('SlackPanel query states', () => {
  it('shows the loading line until the config query settles', async () => {
    vi.spyOn(api, 'getSlackConfig').mockReturnValue(new Promise<SlackConfigData>(() => {}))
    vi.spyOn(api, 'getSlackManifest').mockResolvedValue(MANIFEST)
    renderWithProviders(<SlackPanel />)
    expect(screen.getByText('Loading Slack config…')).toBeInTheDocument()
  })

  it('shows the gateway hint when the config query fails', async () => {
    vi.spyOn(api, 'getSlackConfig').mockRejectedValue(new Error('offline'))
    vi.spyOn(api, 'getSlackManifest').mockResolvedValue(MANIFEST)
    renderWithProviders(<SlackPanel />)
    expect(
      await screen.findByText('Cannot load Slack config. Is the gateway running?', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })
})

/* ── status pill + connection hint ────────────────────────────────────────── */

describe('SlackPanel connection status', () => {
  it('reads Connected with no hint when the gateway holds a live socket', async () => {
    seed({ connected: true })
    await hydrated()
    expect(screen.getByText('Connected')).toBeInTheDocument()
    expect(screen.queryByText(/Click Reconnect to connect/)).not.toBeInTheDocument()
  })

  it('reads Needs setup when no credentials are configured yet', async () => {
    seed({ configured: false })
    await hydrated()
    expect(screen.getByText('Needs setup')).toBeInTheDocument()
    expect(screen.queryByText(/not yet active/)).not.toBeInTheDocument()
  })

  it('explains a saved-but-inactive channel as needing a reconnect', async () => {
    seed()
    await hydrated()
    expect(screen.getByText('Not connected')).toBeInTheDocument()
    expect(
      screen.getByText('Tokens are saved but not yet active. Click Reconnect to connect.'),
    ).toBeInTheDocument()
  })

  it('calls out rejected credentials for invalid_auth specifically', async () => {
    seed({ connect_error: 'invalid_auth' })
    await hydrated()
    expect(screen.getByText(/Slack rejected the stored tokens \(invalid_auth\)/)).toBeInTheDocument()
  })

  it('surfaces any other startup failure with its own error string', async () => {
    seed({ connect_error: 'dns_failure' })
    await hydrated()
    expect(screen.getByText(/Slack connection failed \(dns_failure\)/)).toBeInTheDocument()
  })
})

/* ── reconnect ────────────────────────────────────────────────────────────── */

type ReconnectResult = Awaited<ReturnType<typeof api.reconnectSlack>>

/** The reconnect button, present only on a writable session. */
function reconnectBtn() {
  return screen.getByRole('button', { name: /^Reconnect$/ })
}

describe('SlackPanel reconnect', () => {
  it('re-runs the handshake in place and confirms the connection', async () => {
    seed()
    const reconnect = vi
      .spyOn(api, 'reconnectSlack')
      .mockResolvedValue({ connected: true, connect_error: '' })
    await hydrated()
    // The saved-but-inactive hint is what the button answers.
    expect(screen.getByText(/Click Reconnect to connect/)).toBeInTheDocument()

    fireEvent.click(reconnectBtn())

    await waitFor(() => expect(reconnect).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('Connected to Slack.')).toBeInTheDocument()
    // The outcome supersedes the hint; the badge follows the refetched GET.
    expect(screen.queryByText(/Click Reconnect to connect/)).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows a pending label and disables the button while the handshake runs', async () => {
    seed()
    let settle: (v: ReconnectResult) => void = () => {}
    vi.spyOn(api, 'reconnectSlack').mockReturnValue(new Promise<ReconnectResult>(r => { settle = r }))
    await hydrated()

    fireEvent.click(reconnectBtn())

    const pending = await screen.findByRole('button', { name: 'Reconnecting…' })
    expect(pending).toBeDisabled()
    // A second click mid-flight is inert: the disabled button never fires.
    fireEvent.click(pending)
    expect(api.reconnectSlack).toHaveBeenCalledTimes(1)

    await act(async () => { settle({ connected: true, connect_error: '' }) })
    expect(await screen.findByRole('button', { name: 'Reconnect' })).toBeEnabled()
  })

  it('explains a rejected reconnect inside the panel, in the same words as a startup failure', async () => {
    seed()
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: false, connect_error: 'invalid_auth' })
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    // Labelled as the click's outcome: the reason text may be exactly what was
    // on screen before the click, and the prefix is what shows the attempt ran.
    expect(notice).toHaveTextContent(/^Reconnect failed: Slack rejected the stored tokens \(invalid_auth\)/)
    expect(notice).toHaveTextContent(/then click Reconnect/)
    expect(screen.queryByText('Connected to Slack.')).not.toBeInTheDocument()
  })

  it('names the gateway-side reasons a reconnect declines', async () => {
    seed({ configured: false })
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: false, connect_error: 'owner_id_missing' })
    await hydrated()

    fireEvent.click(reconnectBtn())

    expect(await screen.findByRole('alert')).toHaveTextContent(/owner Slack member ID is missing/)
  })

  it('explains an aborted reconnect as unchanged, with the retry and the fallback', async () => {
    seed()
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: false, connect_error: 'previous_client_close_failed' })
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent(/^Reconnect failed: The previous Slack connection could not be closed, so Slack was left disabled: it authorizes nobody and cannot reply\./)
    expect(notice).toHaveTextContent(/Restart the gateway to recover; clicking Reconnect again may also succeed\./)
    expect(screen.queryByText('Connected to Slack.')).not.toBeInTheDocument()
  })

  it('explains a refused reconnect whose workspace Slack did not confirm, as kept and retryable', async () => {
    seed()
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: false, connect_error: 'workspace_identity_unverified' })
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent(/^Reconnect failed: Slack did not confirm which workspace these tokens belong to/)
    expect(notice).toHaveTextContent(/nothing was connected and the saved Slack conversations were kept\. Click Reconnect again\./)
    // The raw code never reaches the user.
    expect(notice).not.toHaveTextContent('workspace_identity_unverified')
  })

  it('explains a refused reconnect whose workspace record could not be written, with the remedy', async () => {
    seed()
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: false, connect_error: 'workspace_identity_unrecorded' })
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent(/^Reconnect failed: Kiro Crew could not save which Slack workspace these tokens belong to, so nothing was connected\./)
    expect(notice).toHaveTextContent(/Check that the Kiro Crew data folder is writable, then click Reconnect again\./)
    expect(notice).not.toHaveTextContent('workspace_identity_unrecorded')
  })

  it('explains a refused reconnect whose workspace record is damaged, naming the file and the remedy', async () => {
    seed()
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: false, connect_error: 'workspace_record_unreadable' })
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent(/^Reconnect failed: Kiro Crew found a damaged record of which Slack workspace the saved conversations belong to, so nothing was connected\./)
    expect(notice).toHaveTextContent(/Repair or remove slack_workspace\.json in the Kiro Crew data folder, then click Reconnect again\./)
    expect(notice).not.toHaveTextContent('workspace_record_unreadable')
  })

  it('explains a refused switch that has too many saved conversations to set aside, with the remedy', async () => {
    seed()
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: false, connect_error: 'workspace_switch_too_large' })
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent(/^Reconnect failed: These tokens belong to a different Slack workspace, and this install has more saved Slack conversations than Kiro Crew can safely set aside in one switch, so nothing was changed\./)
    expect(notice).toHaveTextContent(/Disconnect Slack conversations you no longer need from their chats, then click Reconnect again\./)
    expect(notice).not.toHaveTextContent('workspace_switch_too_large')
  })

  it('explains a refused switch blocked by one saved conversation it cannot set aside, with the remedy', async () => {
    seed()
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: false, connect_error: 'workspace_switch_unrecordable' })
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent(/^Reconnect failed: These tokens belong to a different Slack workspace, and one of the saved Slack conversations has a record Kiro Crew could not set aside safely, so nothing was changed\./)
    expect(notice).toHaveTextContent(/Disconnect that conversation from its chat, then click Reconnect again\./)
    expect(notice).not.toHaveTextContent('workspace_switch_unrecordable')
  })

  it("shows the gateway's own refusal when it answered the request", async () => {
    seed()
    vi.spyOn(api, 'reconnectSlack').mockRejectedValue(
      new ApiError(403, 'read-only from remote sessions (local machine only)', '{"error":"read-only from remote sessions (local machine only)"}'),
    )
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('read-only from remote sessions (local machine only)')
    // A refusal is not a reconnect outcome: no "Reconnect failed:" reading.
    expect(notice).not.toHaveTextContent(/Reconnect failed/)
  })

  it('asks one plain question when the request never got an answer', async () => {
    seed()
    vi.spyOn(api, 'reconnectSlack').mockRejectedValue(new TypeError('Failed to fetch'))
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('Reconnect failed. Is the gateway running?')
    expect(notice).not.toHaveTextContent('Failed to fetch')
  })

  it('shows one problem at a time: the recorded connect error steps aside for a transport failure', async () => {
    // The gateway recorded invalid_auth at boot; then a click never reaches it.
    seed({ connect_error: 'invalid_auth' })
    vi.spyOn(api, 'reconnectSlack')
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce({ connected: false, connect_error: 'invalid_auth' })
    await hydrated()
    expect(screen.getByRole('alert')).toHaveTextContent(/invalid_auth/)

    fireEvent.click(reconnectBtn())

    await screen.findByText('Reconnect failed. Is the gateway running?')
    const alerts = screen.getAllByRole('alert')
    expect(alerts).toHaveLength(1)
    expect(alerts[0]).not.toHaveTextContent(/invalid_auth/)
    // Not closable -- like the recorded reading and the save's own error, it
    // clears when the next click gets an answer, so the two notices behave
    // as one kind of warning.
    expect(within(alerts[0]).queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument()

    fireEvent.click(reconnectBtn())

    const answered = await screen.findByRole('alert')
    expect(answered).toHaveTextContent(/^Reconnect failed: Slack rejected the stored tokens \(invalid_auth\)/)
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  it('takes the previous failure off screen while a retry is in flight', async () => {
    // A red "failed" beside "Reconnecting…" reads as the retry having failed.
    seed({ connect_error: 'invalid_auth' })
    let settle: (v: ReconnectResult) => void = () => {}
    vi.spyOn(api, 'reconnectSlack').mockReturnValue(new Promise<ReconnectResult>(r => { settle = r }))
    await hydrated()
    expect(screen.getByRole('alert')).toHaveTextContent(/invalid_auth/)

    fireEvent.click(reconnectBtn())

    await screen.findByRole('button', { name: 'Reconnecting…' })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    await act(async () => { settle({ connected: false, connect_error: 'invalid_auth' }) })
    expect(await screen.findByRole('alert')).toHaveTextContent(/^Reconnect failed:/)
  })

  it("shows the gateway's own 500 -- the credential store could not be read -- not a question it already answered", async () => {
    // The gateway answered: its body is its own {error, code} shape. Asking
    // whether it is running would hide the remedy (a readable data folder).
    seed()
    vi.spyOn(api, 'reconnectSlack').mockRejectedValue(
      new ApiError(500, 'could not read the saved Slack credentials', '{"error":"could not read the saved Slack credentials","code":"credential_store_unreadable"}'),
    )
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('could not read the saved Slack credentials')
    expect(notice).not.toHaveTextContent(/Is the gateway running/)
    expect(notice).not.toHaveTextContent('credential_store_unreadable')
  })

  it("shows the gateway's own 503 -- no Slack socket owner -- not a question it already answered", async () => {
    seed()
    vi.spyOn(api, 'reconnectSlack').mockRejectedValue(
      new ApiError(503, 'Slack reconnect unavailable', '{"error":"Slack reconnect unavailable","code":"slack_reconnect_unavailable"}'),
    )
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('Slack reconnect unavailable')
    expect(notice).not.toHaveTextContent(/Is the gateway running/)
  })

  it('treats an unstructured 5xx as no answer: a proxy body is not the gateway speaking', async () => {
    // Same status as the gateway's own 500 above; the body's shape is what
    // tells a proxy that could not reach the gateway from the gateway itself.
    seed()
    vi.spyOn(api, 'reconnectSlack').mockRejectedValue(
      new ApiError(500, 'Error occurred while trying to proxy: localhost:3000/api/slack/reconnect', 'Error occurred while trying to proxy'),
    )
    await hydrated()

    fireEvent.click(reconnectBtn())

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('Reconnect failed. Is the gateway running?')
    expect(notice).not.toHaveTextContent(/proxy/)
  })

  it('is hidden on a read-only remote session, like Save', async () => {
    seed({ read_only: true })
    await hydrated()
    expect(screen.queryByRole('button', { name: /Reconnect/ })).not.toBeInTheDocument()
  })

  it('points a saved credential change at Reconnect, not at a restart', async () => {
    seed({}, { save: { ok: true, restart_required: false, reconnect_required: true, verify_warning: '' } })
    await hydrated()

    fireEvent.click(saveBtn())

    expect(
      await screen.findByText('Saved. Click Reconnect to apply.', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Restart the gateway/)).not.toBeInTheDocument()
    // The confirmation names a button; the button is right beside it, not only
    // in the header above a long form.
    expect(screen.getByTestId('slack-reconnect-after-save')).toHaveTextContent('Reconnect')
    expect(screen.getAllByRole('button', { name: /^Reconnect$/ })).toHaveLength(2)
  })

  it('names both steps when one save changed a token and the slash command', async () => {
    seed({}, { save: { ok: true, restart_required: true, reconnect_required: true, verify_warning: '' } })
    await hydrated()

    fireEvent.click(saveBtn())

    const note = await screen.findByText(/^Saved\. Click Reconnect to apply the tokens; the slash command still needs a gateway restart\.$/, undefined, { timeout: 5_000 })
    expect(note).toBeInTheDocument()
    expect(screen.getByTestId('slack-reconnect-after-save')).toBeInTheDocument()
  })

  it('removes the save-row Reconnect once a reconnect succeeds', async () => {
    seed({}, { save: { ok: true, restart_required: false, reconnect_required: true, verify_warning: '' } })
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: true, connect_error: '' })
    await hydrated()

    fireEvent.click(saveBtn())
    const afterSave = await screen.findByTestId('slack-reconnect-after-save', undefined, { timeout: 5_000 })

    fireEvent.click(afterSave)

    expect(await screen.findByText('Connected to Slack.')).toBeInTheDocument()
    expect(screen.queryByTestId('slack-reconnect-after-save')).not.toBeInTheDocument()
    expect(screen.getByTestId('slack-reconnect')).toBeInTheDocument()
  })

  it('keeps the save-row Reconnect across a later config-only save', async () => {
    // Credentials saved (reconnect pending), then only the slash command is
    // changed: the pending reminder must survive, the tokens are still not live.
    const { save } = seed({}, { save: { ok: true, restart_required: false, reconnect_required: true, verify_warning: '' } })
    await hydrated()

    fireEvent.click(saveBtn())
    await screen.findByTestId('slack-reconnect-after-save', undefined, { timeout: 5_000 })

    save.mockResolvedValue({ ok: true, restart_required: true, reconnect_required: false, verify_warning: '' })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(2))
    // The confirmation still names both pending steps, and the button stays.
    expect(
      await screen.findByText(/^Saved\. Click Reconnect to apply the tokens; the slash command still needs a gateway restart\.$/, undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
    expect(screen.getByTestId('slack-reconnect-after-save')).toBeInTheDocument()
  })

  it('keeps the save-row Reconnect when the reconnect fails', async () => {
    seed({}, { save: { ok: true, restart_required: false, reconnect_required: true, verify_warning: '' } })
    vi.spyOn(api, 'reconnectSlack').mockResolvedValue({ connected: false, connect_error: 'invalid_auth' })
    await hydrated()

    fireEvent.click(saveBtn())
    fireEvent.click(await screen.findByTestId('slack-reconnect-after-save', undefined, { timeout: 5_000 }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/^Reconnect failed:/)
    expect(screen.getByTestId('slack-reconnect-after-save')).toBeInTheDocument()
  })
})

/* ── manifest card ────────────────────────────────────────────────────────── */

describe('SlackPanel manifest card', () => {
  it('links the one-click create URL and copies the YAML, reverting the pill after 1.5s', async () => {
    const writeText = stubClipboard(() => Promise.resolve())
    seed()
    await hydrated()

    expect(screen.getByRole('link', { name: 'Create Slack app' })).toHaveAttribute('href', MANIFEST.create_url)
    expect(screen.getByText(/named KiroCrew-tester/)).toBeInTheDocument()  // brand-ok: product emits KiroCrew-<alias> (slack-manifest.yaml)

    fireEvent.click(screen.getByRole('button', { name: 'Copy manifest YAML' }))
    expect(writeText).toHaveBeenCalledWith(MANIFEST.manifest)
    expect(await screen.findByText('Copied', undefined, { timeout: 5_000 })).toBeInTheDocument()

    await act(async () => { vi.advanceTimersByTime(1_600) })
    expect(screen.getByRole('button', { name: 'Copy manifest YAML' })).toBeInTheDocument()
  })

  it('keeps the pill unchanged when the clipboard write is refused', async () => {
    stubClipboard(() => Promise.reject(new Error('clipboard denied')))
    seed()
    await hydrated()

    fireEvent.click(screen.getByRole('button', { name: 'Copy manifest YAML' }))
    await act(async () => { vi.advanceTimersByTime(50) })
    expect(screen.getByRole('button', { name: 'Copy manifest YAML' })).toBeInTheDocument()
    expect(screen.queryByText('Copied')).not.toBeInTheDocument()
  })

  it('inertly disables create + copy while the manifest is unavailable', async () => {
    const writeText = stubClipboard(() => Promise.resolve())
    seed({}, { manifestFails: true })
    await hydrated()

    const create = screen.getByRole('link', { name: 'Create Slack app' })
    expect(create).toHaveAttribute('href', '#')
    expect(create).toHaveAttribute('aria-disabled', 'true')

    const copy = screen.getByRole('button', { name: 'Copy manifest YAML' })
    expect(copy).toBeDisabled()
    expect(screen.getByText(/named KiroCrew-you/)).toBeInTheDocument()  // brand-ok: product emits KiroCrew-<alias> (slack-manifest.yaml)

    fireEvent.click(copy)
    expect(writeText).not.toHaveBeenCalled()
  })
})

/* ── read-only remote session ─────────────────────────────────────────────── */

describe('SlackPanel read-only session', () => {
  it('drops every writer and shows stored previews only', async () => {
    seed({
      read_only: true,
      bot_token_set: true,
      bot_token_preview: 'xoxb-••••aaaa',
      allowed_enterprise_ids: [],
    })
    await hydrated()

    expect(
      screen.getByText(
        'Slack settings are managed on the machine running Kiro Crew and are read-only from remote sessions.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save Slack settings' })).not.toBeInTheDocument()
    expect(screen.getByText('xoxb-••••aaaa')).toBeInTheDocument()
    // The app-level credential is unset, so its read-only row says so.
    expect(screen.getByText('(not set)')).toBeInTheDocument()
    // An empty allowlist under read-only renders the placeholder, not an editor.
    expect(screen.getByText('(none)')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add' })).not.toBeInTheDocument()
  })

  it('hides the per-tag remove button while read-only', async () => {
    seed({ read_only: true, allowed_enterprise_ids: ['E000ONE'] })
    await hydrated()
    expect(screen.getByText('E000ONE')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove E000ONE' })).not.toBeInTheDocument()
  })
})

/* ── TagListEditor ────────────────────────────────────────────────────────── */

describe('SlackPanel enterprise allowlist editor', () => {
  /** The allowlist's own text input, keyed off its placeholder. */
  function tagInput() {
    return screen.getByPlaceholderText('E0123ABC456')
  }

  it('adds an org on click and keeps Add disabled while the draft is blank', async () => {
    seed()
    await hydrated()

    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
    fireEvent.change(tagInput(), { target: { value: ' T000TWO ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    expect(screen.getByText('T000TWO')).toBeInTheDocument()
    expect(tagInput()).toHaveValue('')
  })

  it('adds on Enter and rejects an ID that fails the E/T shape', async () => {
    seed()
    await hydrated()

    fireEvent.change(tagInput(), { target: { value: 'lower' } })
    fireEvent.keyDown(tagInput(), { key: 'Enter' })
    expect(await screen.findByRole('alert', undefined, { timeout: 5_000 })).toHaveTextContent(
      'is not a valid ID',
    )
    expect(screen.queryByText('lower')).not.toBeInTheDocument()

    // A valid one clears the notice and lands as a tag.
    fireEvent.change(tagInput(), { target: { value: 'E000TWO' } })
    fireEvent.keyDown(tagInput(), { key: 'Enter' })
    expect(screen.getByText('E000TWO')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('ignores a blank submit and de-dupes an org already on the list', async () => {
    seed()
    await hydrated()

    fireEvent.keyDown(tagInput(), { key: 'Enter' })
    fireEvent.keyDown(tagInput(), { key: 'a' })
    expect(screen.getAllByText(/^E000ONE$/)).toHaveLength(1)

    fireEvent.change(tagInput(), { target: { value: 'E000ONE' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(screen.getAllByText(/^E000ONE$/)).toHaveLength(1)
    expect(tagInput()).toHaveValue('')
  })

  it('removes a tag and sends the shortened list on save', async () => {
    const { save } = seed({ allowed_enterprise_ids: ['E000ONE', 'T000TWO'] })
    await hydrated()

    fireEvent.click(screen.getByRole('button', { name: 'Remove E000ONE' }))
    expect(screen.queryByText('E000ONE')).not.toBeInTheDocument()

    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ allowed_enterprise_ids: ['T000TWO'] })
  })
})

/* ── save payload ─────────────────────────────────────────────────────────── */

describe('SlackPanel save payload', () => {
  it('trims the identity fields, forwards both toggles, and reports success', async () => {
    const { save } = seed()
    await hydrated()

    fireEvent.change(screen.getByPlaceholderText('U0123ABC456'), { target: { value: '  U000NEW  ' } })
    fireEvent.change(screen.getByPlaceholderText('kirocrew'), { target: { value: ' crew ' } })
    fireEvent.click(screen.getByRole('switch', { name: 'Phase reactions' }))
    fireEvent.click(screen.getByRole('switch', { name: 'Show thinking' }))
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toEqual({
      owner_id: 'U000NEW',
      command: 'crew',
      allowed_enterprise_ids: ['E000ONE'],
      reactions_enabled: true,
      show_thinking: true,
      session_folder: '',
    })
    expect(await screen.findByText('Saved.', undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('falls back to the channel name when the folder is on but unnamed', async () => {
    const { save } = seed()
    await hydrated()

    fireEvent.click(screen.getByRole('switch', { name: 'File sessions in a folder' }))
    expect(screen.getByPlaceholderText('Slack')).toHaveValue('')
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ session_folder: 'Slack' })
  })

  it('derives the folder toggle from a stored name and saves an edited one', async () => {
    const { save } = seed({ session_folder: 'Inbox' })
    await hydrated()

    expect(screen.getByRole('switch', { name: 'File sessions in a folder' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    fireEvent.change(screen.getByPlaceholderText('Slack'), { target: { value: ' Team Inbox ' } })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ session_folder: 'Team Inbox' })
  })

  it('keeps the folder name out of the payload once the toggle is switched off', async () => {
    const { save } = seed({ session_folder: 'Inbox' })
    await hydrated()

    fireEvent.click(screen.getByRole('switch', { name: 'File sessions in a folder' }))
    expect(screen.queryByPlaceholderText('Slack')).not.toBeInTheDocument()
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ session_folder: '' })
  })

  it('sends pasted credentials and confirms they were verified with Slack', async () => {
    // A credential write is applied by Reconnect (reconnect_required), never a restart.
    const { save } = seed({}, { save: { ok: true, restart_required: false, reconnect_required: true, verify_warning: '' } })
    await hydrated()

    fireEvent.change(screen.getByLabelText('Slack bot token'), { target: { value: ' xoxb-not-a-real-value ' } })
    fireEvent.change(screen.getByLabelText('Slack app token'), { target: { value: 'xapp-not-a-real-value' } })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({
      bot_token: 'xoxb-not-a-real-value',
      app_token: 'xapp-not-a-real-value',
    })
    expect(
      await screen.findByText('Verified with Slack and saved. Click Reconnect to connect.', undefined, {
        timeout: 5_000,
      }),
    ).toBeInTheDocument()
  })

  it('wears the schema restart badge on the slash command and nowhere else', async () => {
    // The badge is schema-driven: GET /api/config/schema marks slack.command
    // boot-only and nothing else in the panel, so exactly one pill renders and
    // it sits in the slash-command row, before any save happens.
    seed()
    await hydrated()
    const badge = await screen.findByTestId('restart-required-badge')
    expect(screen.getAllByTestId('restart-required-badge')).toHaveLength(1)
    const card = badge.closest('[data-stagger-index]') ?? badge.parentElement!.parentElement!
    expect(within(card as HTMLElement).getByLabelText('Slash command')).toBeInTheDocument()
  })

  it('omits the restart hint when a verified save applied live', async () => {
    // restart_required=false is the hot-reload path: the tokens verified AND
    // the channel is already running them, so a restart claim would be a lie.
    const { save } = seed({}, { save: { ok: true, restart_required: false, reconnect_required: false, verify_warning: '' } })
    await hydrated()

    fireEvent.change(screen.getByLabelText('Slack bot token'), { target: { value: 'xoxb-not-a-real-value' } })
    fireEvent.change(screen.getByLabelText('Slack app token'), { target: { value: 'xapp-not-a-real-value' } })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(
      await screen.findByText('Verified with Slack and saved.', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
    // the restart-flavoured verified text must not also render (a separate
    // channel-not-running banner may legitimately mention a restart)
    expect(
      screen.queryByText('Verified with Slack and saved. Click Reconnect to connect.'),
    ).not.toBeInTheDocument()
  })

  it('reports a restart-only save when nothing was verified', async () => {
    // restart_required alone is the slash-command path (the one boot-read field).
    seed({}, { save: { ok: true, restart_required: true, reconnect_required: false, verify_warning: '' } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(
      await screen.findByText('Saved. Restart the gateway to apply.', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })

  it('shows the verify warning alongside the saved pill and withholds the verified claim', async () => {
    seed(
      {},
      { save: { ok: true, restart_required: false, reconnect_required: false, verify_warning: 'App-level credential not checked.' } },
    )
    await hydrated()

    fireEvent.change(screen.getByLabelText('Slack bot token'), { target: { value: 'xoxb-not-a-real-value' } })
    fireEvent.click(saveBtn())

    expect(
      await screen.findByText('App-level credential not checked.', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
    expect(screen.getByText('Saved.')).toBeInTheDocument()
  })

  it('folds Remove into clear flags rather than empty credential strings', async () => {
    const { save } = seed({
      bot_token_set: true,
      app_token_set: true,
      bot_token_preview: 'xoxb-••••aaaa',
      app_token_preview: 'xapp-••••bbbb',
    })
    await hydrated()

    // Two stored credentials, so two identically-labelled Remove buttons: bot
    // first in DOM order, app second.
    const removes = screen.getAllByRole('button', { name: 'Remove' })
    expect(removes).toHaveLength(2)
    fireEvent.click(removes[0])
    fireEvent.click(removes[1])
    expect(screen.getAllByText('Will be removed on save.')).toHaveLength(2)

    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    const payload = save.mock.calls[0][0]
    expect(payload).toMatchObject({ bot_token_clear: true, app_token_clear: true })
    expect(payload.bot_token).toBeUndefined()
    expect(payload.app_token).toBeUndefined()
  })

  it('replaces a stored credential in place, keeping the clear flag off', async () => {
    const { save } = seed({ bot_token_set: true, bot_token_preview: 'xoxb-••••aaaa' })
    await hydrated()

    fireEvent.click(screen.getByRole('button', { name: 'Replace' }))
    fireEvent.change(screen.getByLabelText('Slack bot token'), { target: { value: 'xoxb-replacement' } })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    const payload = save.mock.calls[0][0]
    expect(payload).toMatchObject({ bot_token: 'xoxb-replacement' })
    expect(payload.bot_token_clear).toBeUndefined()
  })

  it('disables the button and reads Saving… while the write is in flight', async () => {
    const { settle } = seed({}, { save: { pending: true } })
    await hydrated()

    fireEvent.click(saveBtn())
    const pendingBtn = await screen.findByRole('button', { name: 'Saving…' }, { timeout: 5_000 })
    expect(pendingBtn).toBeDisabled()

    await act(async () => { settle(OK) })
    expect(await screen.findByText('Saved.', undefined, { timeout: 5_000 })).toBeInTheDocument()
  })
})

/* ── save failure ─────────────────────────────────────────────────────────── */

describe('SlackPanel save failure', () => {
  it('unwraps the server error field out of a JSON response body', async () => {
    seed({}, { save: { reject: new Error(JSON.stringify({ error: 'bot credential rejected by Slack' })) } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(await screen.findByRole('alert', undefined, { timeout: 5_000 })).toHaveTextContent(
      'bot credential rejected by Slack',
    )
  })

  it('keeps the raw body when the JSON response carries no error field', async () => {
    const body = JSON.stringify({ detail: 'no error key here' })
    seed({}, { save: { reject: new Error(body) } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(await screen.findByRole('alert', undefined, { timeout: 5_000 })).toHaveTextContent(body)
  })

  it('shows a non-JSON error message verbatim', async () => {
    seed({}, { save: { reject: new Error('502 Bad Gateway') } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(await screen.findByRole('alert', undefined, { timeout: 5_000 })).toHaveTextContent('502 Bad Gateway')
  })

  it('falls back to the gateway hint when the rejection is not an Error', async () => {
    seed({}, { save: { reject: 'connection reset' } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(await screen.findByRole('alert', undefined, { timeout: 5_000 })).toHaveTextContent(
      'Save failed. Is the gateway running?',
    )
  })

  it('clears a previous error when the next save is attempted', async () => {
    seed({}, { save: { reject: new Error('502 Bad Gateway') } })
    await hydrated()

    fireEvent.click(saveBtn())
    const alert = await screen.findByRole('alert', undefined, { timeout: 5_000 })
    expect(alert).toHaveTextContent('502 Bad Gateway')

    // handleSave clears the banner before dispatching, so the row is briefly clean.
    vi.mocked(api.saveSlackConfig).mockReturnValue(new Promise<SaveResult>(() => {}))
    fireEvent.click(saveBtn())
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(within(screen.getByRole('button', { name: 'Saving…' })).queryByRole('alert')).toBeNull()
  })
})
