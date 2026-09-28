/**
 * Coverage pass over Settings ▸ iMessage (`pages/settings/IMessagePanel.tsx`).
 *
 * Nothing mounted this panel before — `ChannelsPanel.test.tsx` only asserts the
 * settings nav routes to it — so the file sat at 5.8% (4 of 69 lines) and the
 * frontend per-file coverage gate failed on it. Every branch was unexecuted:
 * both query states, the three status pills, both connection hints, the
 * unsupported-host and read-only notices, the handle validator's email and
 * phone paths, the session-folder folding, and both mutation arms with all
 * four of their message shapes.
 *
 * Harness notes, matching `SettingsWebexPanelCoverage.test.tsx`:
 *  - `api` is a plain object literal, so `vi.spyOn` on the two iMessage methods
 *    is enough; the module stays real and nothing else in the tree is stubbed.
 *  - `renderWithProviders` supplies the QueryClient with `retry: false`, so a
 *    rejected query or mutation surfaces on the first attempt.
 *  - The panel schedules two deferred resets (saved pill 6s, error 8s). On the
 *    real clock those fire after vitest tears the environment down and throw
 *    "window is not defined" as an unhandled error — every test passing and the
 *    run still exiting non-zero. Fake timers keep them off the wall clock.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import { api, type IMessageConfigData } from '../api/client'
import { IMessagePanel } from '../pages/settings/IMessagePanel'

/* ── timers ───────────────────────────────────────────────────────────────── */

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

/* ── fixtures ─────────────────────────────────────────────────────────────── */

type SaveResult = Awaited<ReturnType<typeof api.saveIMessageConfig>>

const OK: SaveResult = { ok: true, restart_required: false, verify_warning: '' }

/** Configured, not connected, on a supported writable host — the branch-rich start. */
function config(over: Partial<IMessageConfigData> = {}): IMessageConfigData {
  return {
    connected: false,
    connect_error: '',
    configured: true,
    read_only: false,
    supported: true,
    enabled: false,
    db_path: '',
    allowed_handles: ['+15550000001'],
    service: 'imessage',
    session_folder: '',
    ...over,
  }
}

interface SeedOpts {
  /** Result of a save, a rejection to drive `onError`, or a never-settling write. */
  save?: SaveResult | { reject: unknown } | { pending: true }
}

/** Install both iMessage API seams and mount the panel. */
function seed(cfgOver: Partial<IMessageConfigData> = {}, opts: SeedOpts = {}) {
  vi.spyOn(api, 'getIMessageConfig').mockResolvedValue(config(cfgOver))

  const save = vi.spyOn(api, 'saveIMessageConfig')
  const want = opts.save ?? OK
  if (want && typeof want === 'object' && 'reject' in want) {
    save.mockRejectedValue(want.reject)
  } else if (want && typeof want === 'object' && 'pending' in want) {
    save.mockReturnValue(new Promise<SaveResult>(() => {}))
  } else {
    save.mockResolvedValue(want as SaveResult)
  }

  const view = renderWithProviders(<IMessagePanel />)
  return { ...view, save }
}

/** Resolve once the query has hydrated and the form is on screen. */
async function hydrated() {
  return await screen.findByRole('heading', { name: 'iMessage', level: 3 }, { timeout: 5_000 })
}

/** The save button, which only exists on a writable supported session. */
function saveBtn() {
  return screen.getByRole('button', { name: 'Save iMessage settings' })
}

/** The allowlist's own text input, keyed off its placeholder. */
function handleInput() {
  return screen.getByPlaceholderText('+15551234567')
}

/* ── query states ─────────────────────────────────────────────────────────── */

describe('IMessagePanel query states', () => {
  it('shows the loading line until the config query settles', () => {
    vi.spyOn(api, 'getIMessageConfig').mockReturnValue(new Promise<IMessageConfigData>(() => {}))
    renderWithProviders(<IMessagePanel />)
    expect(screen.getByText('Loading iMessage config…')).toBeInTheDocument()
  })

  it('shows the gateway hint when the config query fails', async () => {
    vi.spyOn(api, 'getIMessageConfig').mockRejectedValue(new Error('offline'))
    renderWithProviders(<IMessagePanel />)
    expect(
      await screen.findByText('Cannot load iMessage config. Is the gateway running?', undefined, {
        timeout: 5_000,
      }),
    ).toBeInTheDocument()
  })
})

/* ── status pill + connection hint ────────────────────────────────────────── */

describe('IMessagePanel connection status', () => {
  it('reads Active once the channel is connected, with no hint', async () => {
    seed({ connected: true })
    await hydrated()
    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.queryByText(/Restart the gateway to connect/)).not.toBeInTheDocument()
  })

  it('reads Needs setup before anything is configured', async () => {
    seed({ configured: false })
    await hydrated()
    expect(screen.getByText('Needs setup')).toBeInTheDocument()
  })

  it('reads Not active and explains a saved-but-idle channel', async () => {
    seed()
    await hydrated()
    expect(screen.getByText('Not active')).toBeInTheDocument()
    expect(
      screen.getByText(
        'Settings are saved but the channel is not running. Restart the gateway to connect.',
      ),
    ).toBeInTheDocument()
  })

  it('surfaces the bridge error when one was reported', async () => {
    seed({ connect_error: 'imsg not found' })
    await hydrated()
    expect(screen.getByText(/imsg not found/)).toBeInTheDocument()
  })
})

/* ── locked hosts ─────────────────────────────────────────────────────────── */

describe('IMessagePanel locked hosts', () => {
  it('explains an unsupported host and hides the save button', async () => {
    seed({ supported: false })
    await hydrated()
    expect(screen.getByText(/This host cannot reach iMessage/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save iMessage settings' })).not.toBeInTheDocument()
  })

  it('explains a remote session and hides the save button', async () => {
    seed({ read_only: true })
    await hydrated()
    expect(screen.getByText(/read-only from a remote session/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save iMessage settings' })).not.toBeInTheDocument()
  })
})

/* ── handle validator ─────────────────────────────────────────────────────── */

describe('IMessagePanel handle validator', () => {
  it('accepts a phone handle carrying dialling punctuation', async () => {
    seed()
    await hydrated()

    fireEvent.change(handleInput(), { target: { value: '+1 (555) 123-4567' } })
    fireEvent.keyDown(handleInput(), { key: 'Enter' })
    expect(screen.getByText('+1 (555) 123-4567')).toBeInTheDocument()
  })

  it('accepts an Apple Account email', async () => {
    seed()
    await hydrated()

    fireEvent.change(handleInput(), { target: { value: 'someone@example.com' } })
    fireEvent.keyDown(handleInput(), { key: 'Enter' })
    expect(screen.getByText('someone@example.com')).toBeInTheDocument()
  })

  it('rejects a spaced email, a doubled @, a dotless domain, too few digits and a stray character', async () => {
    seed()
    await hydrated()

    for (const bad of [
      'two words@example.com',
      'you@@example.com',
      'you@localhost',
      '+12',
      '+1555abc1234',
    ]) {
      fireEvent.change(handleInput(), { target: { value: bad } })
      fireEvent.keyDown(handleInput(), { key: 'Enter' })
      expect(await screen.findByRole('alert', undefined, { timeout: 5_000 })).toBeInTheDocument()
    }
    // Nothing was accepted: the seeded handle is still the only tag.
    expect(screen.getAllByText(/^\+15550000001$/)).toHaveLength(1)
  })

  it('rejects an over-long handle', async () => {
    seed()
    await hydrated()

    fireEvent.change(handleInput(), { target: { value: `${'a'.repeat(250)}@example.com` } })
    fireEvent.keyDown(handleInput(), { key: 'Enter' })
    expect(await screen.findByRole('alert', undefined, { timeout: 5_000 })).toBeInTheDocument()
  })
})

/* ── empty-allowlist warning ──────────────────────────────────────────────── */

describe('IMessagePanel allowlist warning', () => {
  it('warns when the channel is enabled with nothing allowed', async () => {
    seed({ enabled: true, allowed_handles: [] })
    await hydrated()
    expect(screen.getByText(/it will reject every message/)).toBeInTheDocument()
  })

  it('stays quiet while the channel is off', async () => {
    seed({ enabled: false, allowed_handles: [] })
    await hydrated()
    expect(screen.queryByText(/it will reject every message/)).not.toBeInTheDocument()
  })
})

/* ── save payload ─────────────────────────────────────────────────────────── */

describe('IMessagePanel save payload', () => {
  it('trims the database path and sends the seeded fields', async () => {
    const { save } = seed({ db_path: '  ~/Library/Messages/chat.db  ' })
    await hydrated()

    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({
      enabled: false,
      allowed_handles: ['+15550000001'],
      db_path: '~/Library/Messages/chat.db',
      service: 'imessage',
      session_folder: '',
    })
  })

  it('sends the chosen service', async () => {
    const { save } = seed()
    await hydrated()

    // The service control is a SearchableSelect, not a native <select>: open the
    // combobox by its caption, then pick the option by its label.
    fireEvent.click(screen.getByRole('combobox', { name: 'Send replies over' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Automatic (fall back to SMS)' }))
    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ service: 'auto' })
  })

  it('falls back to the channel name when filing is on with a blank folder', async () => {
    const { save } = seed({ session_folder: 'Texts' })
    await hydrated()

    fireEvent.change(screen.getByDisplayValue('Texts'), { target: { value: '   ' } })
    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ session_folder: 'iMessage' })
  })

  it('sends the empty off-state when filing is switched off', async () => {
    const { save } = seed({ session_folder: 'Texts' })
    await hydrated()

    fireEvent.click(screen.getByLabelText('File sessions in a folder'))
    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ session_folder: '' })
  })
})

/* ── mutation arms ────────────────────────────────────────────────────────── */

describe('IMessagePanel save outcomes', () => {
  it('confirms a save and keeps the edited draft rather than reseeding it', async () => {
    const { save } = seed()
    await hydrated()

    fireEvent.change(handleInput(), { target: { value: '+15559998888' } })
    fireEvent.keyDown(handleInput(), { key: 'Enter' })
    fireEvent.click(saveBtn())

    expect(await screen.findByText('Saved.', undefined, { timeout: 5_000 })).toBeInTheDocument()
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    // The post-save invalidation refetches the seeded config, which does NOT
    // contain this handle. It must still be on screen: the draft stays
    // authoritative after the initial load.
    expect(screen.getByText('+15559998888')).toBeInTheDocument()
  })

  it('asks for a restart when the server says one is required', async () => {
    seed({}, { save: { ok: true, restart_required: true, verify_warning: '' } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(
      await screen.findByText('Saved. Restart the gateway to apply.', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })

  it('shows the pending label while the write is in flight', async () => {
    seed({}, { save: { pending: true } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(await screen.findByText('Saving…', undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('unwraps a JSON error body from the failed write', async () => {
    seed({}, { save: { reject: new Error(JSON.stringify({ error: 'db_path is not readable' })) } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(
      await screen.findByText('db_path is not readable', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })

  it('falls back to a plain error message when the body is not JSON', async () => {
    seed({}, { save: { reject: new Error('bridge unreachable') } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(
      await screen.findByText('bridge unreachable', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })

  it('falls back to the generic notice when the rejection is not an Error', async () => {
    seed({}, { save: { reject: 'nope' } })
    await hydrated()

    fireEvent.click(saveBtn())
    expect(
      await screen.findByText('Save failed. Is the gateway running?', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })
})
