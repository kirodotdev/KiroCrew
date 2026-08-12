/**
 * Coverage pass over Settings ▸ Webex (`pages/settings/WebexPanel.tsx`).
 *
 * Nothing mounted this panel before — `ChannelsPanel.test.tsx` only asserts that
 * the settings nav routes to it — so every branch in the file was unexecuted:
 * the two query states, the three status pills, both connection hints, the
 * read-only remote view, the email allowlist editor (add / reject / dedupe /
 * remove), the save payload's folder and credential folding, and both mutation
 * arms with all four of their message shapes.
 *
 * Harness notes:
 *  - `api` is a plain object literal, so `vi.spyOn` on the two Webex methods is
 *    enough; the module stays real and nothing else in the tree is stubbed.
 *  - `renderWithProviders` supplies the QueryClient with `retry: false`, so a
 *    rejected query or mutation surfaces on the first attempt.
 *  - Credentials here are obvious placeholders. The panel only forwards them to
 *    a mocked writer, and `SecretField` never renders a stored secret back, so
 *    there is nothing real to put in this file.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import { api, type WebexConfigData } from '../api/client'
import { WebexPanel } from '../pages/settings/WebexPanel'

/* ── timers ───────────────────────────────────────────────────────────────── */

// The panel schedules two deferred resets (saved pill 6s, save error 8s). On the
// real clock those callbacks fire after vitest tears the environment down and
// throw "window is not defined" as an UNHANDLED error — every test passing and
// the run still exiting non-zero. Fake timers keep them off the wall clock and
// `clearAllTimers` drops the pending ones at teardown; `shouldAdvanceTime` keeps
// the clock moving so `findBy*` behaves as it does with real timers.
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

/* ── fixtures ─────────────────────────────────────────────────────────────── */

type SaveResult = Awaited<ReturnType<typeof api.saveWebexConfig>>

const OK: SaveResult = { ok: true, restart_required: false, verify_warning: '' }

const CREATE_BOT_URL = 'https://developer.webex.com/my-apps/new/bot'
const SETUP_GUIDE =
  'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/webex-integration.md'

/** Configured, not connected, nothing stored — the most branch-rich start. */
function config(over: Partial<WebexConfigData> = {}): WebexConfigData {
  return {
    connected: false,
    connect_error: '',
    configured: true,
    read_only: false,
    bot_token_set: false,
    bot_token_preview: '',
    enabled: false,
    allowed_emails: ['first@example.com'],
    ...over,
  }
}

interface SeedOpts {
  /** Result of a save, a rejection to drive `onError`, or a never-settling write. */
  save?: SaveResult | { reject: unknown } | { pending: true }
}

/** Install both Webex API seams and mount the panel. */
function seed(cfgOver: Partial<WebexConfigData> = {}, opts: SeedOpts = {}) {
  vi.spyOn(api, 'getWebexConfig').mockResolvedValue(config(cfgOver))

  const save = vi.spyOn(api, 'saveWebexConfig')
  let settle: (v: SaveResult) => void = () => {}
  const want = opts.save ?? OK
  if (want && typeof want === 'object' && 'reject' in want) {
    save.mockRejectedValue(want.reject)
  } else if (want && typeof want === 'object' && 'pending' in want) {
    save.mockReturnValue(new Promise<SaveResult>(r => { settle = r }))
  } else {
    save.mockResolvedValue(want as SaveResult)
  }

  const view = renderWithProviders(<WebexPanel />)
  return { ...view, save, settle }
}

/** Resolve once the query has hydrated and the form is on screen. */
async function hydrated() {
  return await screen.findByRole('heading', { name: 'Webex', level: 3 }, { timeout: 5_000 })
}

/** The save button, which only exists on a writable session. */
function saveBtn() {
  return screen.getByRole('button', { name: 'Save Webex settings' })
}

/** The allowlist's own text input, keyed off its placeholder. */
function emailInput() {
  return screen.getByPlaceholderText('you@example.com')
}

/* ── query states ─────────────────────────────────────────────────────────── */

describe('WebexPanel query states', () => {
  it('shows the loading line until the config query settles', () => {
    vi.spyOn(api, 'getWebexConfig').mockReturnValue(new Promise<WebexConfigData>(() => {}))
    renderWithProviders(<WebexPanel />)
    expect(screen.getByText('Loading Webex config…')).toBeInTheDocument()
  })

  it('shows the gateway hint when the config query fails', async () => {
    vi.spyOn(api, 'getWebexConfig').mockRejectedValue(new Error('offline'))
    renderWithProviders(<WebexPanel />)
    expect(
      await screen.findByText('Cannot load Webex config. Is the gateway running?', undefined, {
        timeout: 5_000,
      }),
    ).toBeInTheDocument()
  })
})

/* ── status pill + connection hint ────────────────────────────────────────── */

describe('WebexPanel connection status', () => {
  it('reads Active with no hint when the gateway holds a live socket', async () => {
    seed({ connected: true })
    await hydrated()
    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.queryByText(/Restart the gateway to connect/)).not.toBeInTheDocument()
  })

  it('reads Needs setup and stays silent when no credential is configured', async () => {
    seed({ configured: false })
    await hydrated()
    expect(screen.getByText('Needs setup')).toBeInTheDocument()
    expect(screen.queryByText(/Restart the gateway to connect/)).not.toBeInTheDocument()
  })

  it('explains a saved-but-inactive channel as needing a restart', async () => {
    seed()
    await hydrated()
    expect(screen.getByText('Not active')).toBeInTheDocument()
    expect(
      screen.getByText(
        'Settings are saved but the channel is not running. Restart the gateway to connect.',
      ),
    ).toBeInTheDocument()
  })

  it('surfaces a startup failure with its own error string', async () => {
    seed({ connect_error: 'dns_failure' })
    await hydrated()
    expect(screen.getByText(/Webex connection failed \(dns_failure\)/)).toBeInTheDocument()
  })
})

/* ── credentials guide ────────────────────────────────────────────────────── */

describe('WebexPanel credentials guide', () => {
  it('links the developer portal, the setup guide, and the token help icon', async () => {
    seed()
    await hydrated()

    expect(screen.getByRole('link', { name: /Create Webex bot/ })).toHaveAttribute(
      'href',
      CREATE_BOT_URL,
    )
    expect(screen.getByRole('link', { name: /^Setup guide/ })).toHaveAttribute('href', SETUP_GUIDE)
    expect(screen.getByRole('link', { name: 'Where to find the bot token' })).toHaveAttribute(
      'href',
      SETUP_GUIDE,
    )
  })
})

/* ── read-only remote session ─────────────────────────────────────────────── */

describe('WebexPanel read-only session', () => {
  it('drops every writer and shows the stored preview only', async () => {
    seed({
      read_only: true,
      bot_token_set: true,
      bot_token_preview: 'Zm9v••••abcd',
      allowed_emails: [],
    })
    await hydrated()

    expect(
      screen.getByText(
        'Webex settings are managed on the machine running Kiro Crew and are read-only from remote sessions.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save Webex settings' })).not.toBeInTheDocument()
    expect(screen.getByText('Zm9v••••abcd')).toBeInTheDocument()
    // An empty allowlist under read-only renders the placeholder, not an editor.
    expect(screen.getByText('(none)')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add' })).not.toBeInTheDocument()
  })

  it('renders the unset credential row and hides the per-tag remove button', async () => {
    seed({ read_only: true, allowed_emails: ['remote@example.com'] })
    await hydrated()

    expect(screen.getByText('(not set)')).toBeInTheDocument()
    expect(screen.getByText('remote@example.com')).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Remove remote@example.com' }),
    ).not.toBeInTheDocument()
  })
})

/* ── allowed-email editor ─────────────────────────────────────────────────── */

describe('WebexPanel allowed-email editor', () => {
  it('adds an address on click and keeps Add disabled while the draft is blank', async () => {
    seed()
    await hydrated()

    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
    fireEvent.change(emailInput(), { target: { value: '  second@example.com  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    expect(screen.getByText('second@example.com')).toBeInTheDocument()
    expect(emailInput()).toHaveValue('')
  })

  it('adds on Enter and rejects an address with no @ sign', async () => {
    seed()
    await hydrated()

    fireEvent.change(emailInput(), { target: { value: 'nobody' } })
    fireEvent.keyDown(emailInput(), { key: 'Enter' })
    expect(await screen.findByRole('alert', undefined, { timeout: 5_000 })).toHaveTextContent(
      'is not a valid ID',
    )
    expect(screen.queryByText('nobody')).not.toBeInTheDocument()

    // A well-shaped one clears the notice and lands as a tag.
    fireEvent.change(emailInput(), { target: { value: 'second@example.com' } })
    fireEvent.keyDown(emailInput(), { key: 'Enter' })
    expect(screen.getByText('second@example.com')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('rejects whitespace, a leading @, a doubled @, a dotless domain and an over-long address', async () => {
    seed()
    await hydrated()

    for (const bad of [
      'two words@example.com',
      '@example.com',
      'you@@example.com',
      'you@localhost',
      `${'a'.repeat(250)}@example.com`,
    ]) {
      fireEvent.change(emailInput(), { target: { value: bad } })
      fireEvent.keyDown(emailInput(), { key: 'Enter' })
      expect(await screen.findByRole('alert', undefined, { timeout: 5_000 })).toHaveTextContent(
        'is not a valid ID',
      )
    }
    // Nothing was accepted: the seeded address is still the only tag.
    expect(screen.getByText('first@example.com')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove first@example.com' })).toBeInTheDocument()
  })

  it('ignores a blank submit and de-dupes an address already on the list', async () => {
    seed()
    await hydrated()

    fireEvent.keyDown(emailInput(), { key: 'Enter' })
    fireEvent.keyDown(emailInput(), { key: 'a' })
    expect(screen.getAllByText(/^first@example\.com$/)).toHaveLength(1)

    fireEvent.change(emailInput(), { target: { value: 'first@example.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(screen.getAllByText(/^first@example\.com$/)).toHaveLength(1)
    expect(emailInput()).toHaveValue('')
  })

  it('removes a tag and sends the shortened list on save', async () => {
    const { save } = seed({ allowed_emails: ['first@example.com', 'second@example.com'] })
    await hydrated()

    fireEvent.click(screen.getByRole('button', { name: 'Remove first@example.com' }))
    expect(screen.queryByText('first@example.com')).not.toBeInTheDocument()

    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ allowed_emails: ['second@example.com'] })
  })
})

/* ── save payload ─────────────────────────────────────────────────────────── */

describe('WebexPanel save payload', () => {
  it('forwards the enable toggle with an unfiled folder and reports success', async () => {
    const { save } = seed()
    await hydrated()

    fireEvent.click(screen.getByRole('switch', { name: 'Enable Webex channel' }))
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toEqual({
      enabled: true,
      allowed_emails: ['first@example.com'],
      session_folder: '',
    })
    expect(await screen.findByText('Saved.', undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('falls back to the channel name when the folder is on but unnamed', async () => {
    const { save } = seed()
    await hydrated()

    fireEvent.click(screen.getByRole('switch', { name: 'File sessions in a folder' }))
    expect(screen.getByPlaceholderText('Webex')).toHaveValue('')
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ session_folder: 'Webex' })
  })

  it('derives the folder toggle from a stored name and saves an edited one trimmed', async () => {
    const { save } = seed({ session_folder: 'Inbox' })
    await hydrated()

    expect(screen.getByRole('switch', { name: 'File sessions in a folder' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    fireEvent.change(screen.getByPlaceholderText('Webex'), { target: { value: ' Team Inbox ' } })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ session_folder: 'Team Inbox' })
  })

  it('keeps the folder name out of the payload once the toggle is switched off', async () => {
    const { save } = seed({ session_folder: 'Inbox' })
    await hydrated()

    fireEvent.click(screen.getByRole('switch', { name: 'File sessions in a folder' }))
    expect(screen.queryByPlaceholderText('Webex')).not.toBeInTheDocument()
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ session_folder: '' })
  })

  it('sends a pasted credential trimmed and confirms it was verified with Webex', async () => {
    const { save } = seed({}, { save: { ok: true, restart_required: true, verify_warning: '' } })
    await hydrated()

    fireEvent.change(screen.getByLabelText('Webex bot token'), {
      target: { value: '  not-a-real-webex-token  ' },
    })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ bot_token: 'not-a-real-webex-token' })
    expect(
      await screen.findByText('Verified with Webex and saved. Restart the gateway to connect.', undefined, {
        timeout: 5_000,
      }),
    ).toBeInTheDocument()
  })

  it('reports a restart-only save when no credential was submitted', async () => {
    seed({}, { save: { ok: true, restart_required: true, verify_warning: '' } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(
      await screen.findByText('Saved. Restart the gateway to apply.', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })

  it('shows the verify warning beside the saved pill and withholds the verified claim', async () => {
    seed(
      {},
      { save: { ok: true, restart_required: false, verify_warning: 'Token not checked with Webex.' } },
    )
    await hydrated()

    fireEvent.change(screen.getByLabelText('Webex bot token'), {
      target: { value: 'not-a-real-webex-token' },
    })
    fireEvent.click(saveBtn())

    expect(
      await screen.findByText('Token not checked with Webex.', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
    expect(screen.getByText('Saved.')).toBeInTheDocument()
  })

  it('retires the saved pill six seconds later', async () => {
    seed()
    await hydrated()

    fireEvent.click(saveBtn())
    expect(await screen.findByText('Saved.', undefined, { timeout: 5_000 })).toBeInTheDocument()

    await act(async () => { vi.advanceTimersByTime(6_100) })
    expect(screen.queryByText('Saved.')).not.toBeInTheDocument()
  })

  it('folds Remove into a clear flag rather than an empty credential string', async () => {
    const { save } = seed({ bot_token_set: true, bot_token_preview: 'Zm9v••••abcd' })
    await hydrated()

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(screen.getByText('Will be removed on save.')).toBeInTheDocument()

    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    const payload = save.mock.calls[0][0]
    expect(payload).toMatchObject({ bot_token_clear: true })
    expect(payload.bot_token).toBeUndefined()
  })

  it('replaces a stored credential in place, keeping the clear flag off', async () => {
    const { save } = seed({ bot_token_set: true, bot_token_preview: 'Zm9v••••abcd' })
    await hydrated()

    fireEvent.click(screen.getByRole('button', { name: 'Replace' }))
    fireEvent.change(screen.getByLabelText('Webex bot token'), {
      target: { value: 'replacement-placeholder' },
    })
    fireEvent.click(saveBtn())

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    const payload = save.mock.calls[0][0]
    expect(payload).toMatchObject({ bot_token: 'replacement-placeholder' })
    expect(payload.bot_token_clear).toBeUndefined()
  })

  it('disables the button and reads Saving… while the write is in flight', async () => {
    const { settle } = seed({}, { save: { pending: true } })
    await hydrated()

    fireEvent.click(saveBtn())
    const pending = await screen.findByRole('button', { name: 'Saving…' }, { timeout: 5_000 })
    expect(pending).toBeDisabled()

    await act(async () => { settle(OK) })
    expect(await screen.findByText('Saved.', undefined, { timeout: 5_000 })).toBeInTheDocument()
  })
})

/* ── save failure ─────────────────────────────────────────────────────────── */

describe('WebexPanel save failure', () => {
  it('unwraps the server error field out of a JSON response body', async () => {
    seed({}, { save: { reject: new Error(JSON.stringify({ error: 'token rejected by Webex' })) } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(
      await screen.findByText('token rejected by Webex', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })

  it('keeps the raw body when the JSON response carries no error field', async () => {
    const body = JSON.stringify({ detail: 'no error key here' })
    seed({}, { save: { reject: new Error(body) } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(await screen.findByText(body, undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('shows a non-JSON error message verbatim and retires it after eight seconds', async () => {
    seed({}, { save: { reject: new Error('502 Bad Gateway') } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(
      await screen.findByText('502 Bad Gateway', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()

    await act(async () => { vi.advanceTimersByTime(8_100) })
    expect(screen.queryByText('502 Bad Gateway')).not.toBeInTheDocument()
  })

  it('falls back to the gateway hint when the rejection is not an Error', async () => {
    seed({}, { save: { reject: 'connection reset' } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(
      await screen.findByText('Save failed. Is the gateway running?', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })

  it('falls back to the gateway hint for an Error with an empty message', async () => {
    seed({}, { save: { reject: new Error('') } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(
      await screen.findByText('Save failed. Is the gateway running?', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })
})
