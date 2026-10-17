/**
 * Settings ▸ Signal (`pages/settings/SignalPanel.tsx`).
 *
 * Mirrors `SettingsIMessagePanelCoverage.test.tsx`: both query states, the
 * three status pills, the startup error and hint, the missing-binary and
 * read-only notices, the account and allow-list validators, the save payload
 * (identifiers keep their `+`, no binary-path field is ever sent), and the
 * mutation arms with their message shapes.
 *
 * Harness notes, as in the iMessage file:
 *  - `api` is a plain object literal, so `vi.spyOn` on the two Signal methods
 *    is enough; nothing else in the tree is stubbed.
 *  - `renderWithProviders` supplies a QueryClient with `retry: false`.
 *  - The panel schedules a deferred reset of the saved pill (6s); fake timers
 *    keep it off the wall clock, restored in `afterEach`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import { api, type SignalConfigData } from '../api/client'
import { SignalPanel, isSignalId, isSignalNumber, normalizeSignalId } from '../pages/settings/SignalPanel'

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

type SaveResult = Awaited<ReturnType<typeof api.saveSignalConfig>>

const OK: SaveResult = { ok: true, restart_required: false, verify_warning: '' }
const OWN = '+447700900123'

/** Configured, not connected, on a writable host with signal-cli present. */
function config(over: Partial<SignalConfigData> = {}): SignalConfigData {
  return {
    connected: false,
    connect_error: '',
    configured: true,
    read_only: false,
    cli_found: true,
    enabled: false,
    account: OWN,
    allowed_numbers: [OWN],
    session_folder: '',
    ...over,
  }
}

interface SeedOpts {
  save?: SaveResult | { reject: unknown } | { pending: true }
}

function seed(cfgOver: Partial<SignalConfigData> = {}, opts: SeedOpts = {}) {
  vi.spyOn(api, 'getSignalConfig').mockResolvedValue(config(cfgOver))
  const save = vi.spyOn(api, 'saveSignalConfig')
  const want = opts.save ?? OK
  if (want && typeof want === 'object' && 'reject' in want) {
    save.mockRejectedValue(want.reject)
  } else if (want && typeof want === 'object' && 'pending' in want) {
    save.mockReturnValue(new Promise<SaveResult>(() => {}))
  } else {
    save.mockResolvedValue(want as SaveResult)
  }
  const view = renderWithProviders(<SignalPanel />)
  return { ...view, save }
}

async function hydrated() {
  return await screen.findByRole('heading', { name: 'Signal', level: 3 }, { timeout: 5_000 })
}

function saveBtn() {
  return screen.getByRole('button', { name: 'Save Signal settings' })
}

function accountInput() {
  return screen.getByLabelText('Account number')
}

/** The allow-list's own text input: the tag editor's, not the account field. */
function numberInput() {
  const inputs = screen.getAllByPlaceholderText(OWN)
  return inputs[inputs.length - 1]
}

/* ── identifier helpers ───────────────────────────────────────────────────── */

describe('Signal identifier helpers', () => {
  it('canonicalize formatting and a 00 prefix to +E.164', () => {
    expect(normalizeSignalId(' +44 7700-900123 ')).toBe(OWN)
    expect(normalizeSignalId('0044 7700 900123')).toBe(OWN)
    expect(normalizeSignalId('A1B2C3D4-0000-4000-8000-ABCDEFABCDEF')).toBe('a1b2c3d4-0000-4000-8000-abcdefabcdef')
  })

  it('accept a number for the account but not an ACI', () => {
    expect(isSignalNumber(OWN)).toBe(true)
    expect(isSignalNumber('a1b2c3d4-0000-4000-8000-abcdefabcdef')).toBe(false)
  })

  it('accept a number or an ACI on the allow-list and refuse the rest', () => {
    expect(isSignalId('+1 (555) 010-1234')).toBe(true)
    expect(isSignalId('a1b2c3d4-0000-4000-8000-abcdefabcdef')).toBe(true)
    for (const bad of ['447700900123', '+12345', '+4477009001234567', 'someone@example.com', '']) {
      expect(isSignalId(bad), bad).toBe(false)
    }
  })
})

/* ── query states ─────────────────────────────────────────────────────────── */

describe('SignalPanel query states', () => {
  it('shows the loading line until the config query settles', async () => {
    const get = vi.spyOn(api, 'getSignalConfig').mockReturnValue(new Promise<SignalConfigData>(() => {}))
    renderWithProviders(<SignalPanel />)
    expect(screen.getByText('Loading Signal config…')).toBeInTheDocument()
    // Barrier for the providers' own mount-time updates: the query has been
    // issued, and the line is still up while it stays pending.
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.getByText('Loading Signal config…')).toBeInTheDocument()
  })

  it('shows the gateway hint when the config query fails', async () => {
    vi.spyOn(api, 'getSignalConfig').mockRejectedValue(new Error('offline'))
    renderWithProviders(<SignalPanel />)
    expect(
      await screen.findByText('Cannot load Signal config. Is the gateway running?', undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })
})

/* ── status pill + startup problems ───────────────────────────────────────── */

describe('SignalPanel connection status', () => {
  it('reads Active once the channel is connected, with no hint', async () => {
    seed({ connected: true })
    await hydrated()
    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.queryByText(/not running yet/)).not.toBeInTheDocument()
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
    expect(screen.getByText(/the channel is not running yet/)).toBeInTheDocument()
  })

  it('surfaces signal-cli’s own error instead of the generic hint', async () => {
    seed({ connect_error: 'User +447*** is not registered.' })
    await hydrated()
    expect(screen.getByText(/is not registered/)).toBeInTheDocument()
    expect(screen.queryByText(/the channel is not running yet/)).not.toBeInTheDocument()
  })
})

/* ── notices ──────────────────────────────────────────────────────────────── */

describe('SignalPanel notices', () => {
  it('explains a missing signal-cli and names the environment override', async () => {
    seed({ cli_found: false })
    await hydrated()
    expect(screen.getByText(/signal-cli was not found/)).toBeInTheDocument()
    expect(screen.getByText(/KIROCREW_SIGNAL_CLI/)).toBeInTheDocument()
  })

  it('stays quiet about the binary when one was found', async () => {
    seed()
    await hydrated()
    expect(screen.queryByText(/signal-cli was not found/)).not.toBeInTheDocument()
  })

  it('explains a remote session and hides the save button', async () => {
    seed({ read_only: true })
    await hydrated()
    expect(screen.getByText(/read-only from a remote session/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save Signal settings' })).not.toBeInTheDocument()
  })

  it('describes both setups and links the guide', async () => {
    seed()
    await hydrated()
    expect(screen.getByText('Dedicated bot number')).toBeInTheDocument()
    expect(screen.getByText('Linked device, using Note to Self')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Setup guide/ })).toHaveAttribute(
      'href',
      expect.stringContaining('signal-integration.md'),
    )
  })
})

/* ── validation hints ─────────────────────────────────────────────────────── */

describe('SignalPanel validation', () => {
  it('flags an account without its + and blocks the save', async () => {
    seed()
    await hydrated()
    fireEvent.change(accountInput(), { target: { value: '447700900123' } })
    expect(screen.getByText(/with a leading \+, for example/)).toBeInTheDocument()
    expect(saveBtn()).toBeDisabled()
  })

  it('refuses a malformed allow-list entry', async () => {
    seed()
    await hydrated()
    fireEvent.change(numberInput(), { target: { value: '15550101234' } })
    fireEvent.keyDown(numberInput(), { key: 'Enter' })
    expect(await screen.findByRole('alert', undefined, { timeout: 5_000 })).toBeInTheDocument()
    expect(screen.queryByText('15550101234')).not.toBeInTheDocument()
  })

  it('accepts a formatted number on the allow-list', async () => {
    seed()
    await hydrated()
    fireEvent.change(numberInput(), { target: { value: '+1 555 010 1234' } })
    fireEvent.keyDown(numberInput(), { key: 'Enter' })
    expect(screen.getByText('+1 555 010 1234')).toBeInTheDocument()
  })

  it('warns when the channel is enabled with nothing allowed', async () => {
    seed({ enabled: true, allowed_numbers: [] })
    await hydrated()
    expect(screen.getByText(/it will answer nobody/)).toBeInTheDocument()
  })

  it('warns when the channel is enabled with no account', async () => {
    seed({ enabled: true, account: '' })
    await hydrated()
    expect(screen.getByText(/no account is set/)).toBeInTheDocument()
  })

  it('suggests listing the account for Note to Self only when it is missing from the list', async () => {
    seed({ allowed_numbers: ['+15550101234'] })
    await hydrated()
    expect(screen.getByText(/to chat in Note to Self/)).toBeInTheDocument()
  })

  it('does not suggest it once the account is allowed', async () => {
    seed({ allowed_numbers: ['+44 7700 900123'] })
    await hydrated()
    expect(screen.queryByText(/to chat in Note to Self/)).not.toBeInTheDocument()
  })
})

/* ── save payload ─────────────────────────────────────────────────────────── */

describe('SignalPanel save payload', () => {
  it('sends the identifiers as strings with their + and no binary-path field', async () => {
    const { save } = seed({ allowed_numbers: [OWN, '+15550101234'] })
    await hydrated()

    fireEvent.change(accountInput(), { target: { value: `  ${OWN}  ` } })
    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    const body = save.mock.calls[0][0]
    expect(body).toEqual({
      enabled: false,
      account: OWN,
      allowed_numbers: [OWN, '+15550101234'],
      session_folder: '',
    })
    expect(Object.keys(body)).not.toContain('cli_path')
  })

  it('sends the toggled enabled flag', async () => {
    const { save } = seed()
    await hydrated()
    fireEvent.click(screen.getByLabelText('Enable Signal channel'))
    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ enabled: true })
  })

  it('falls back to the channel name when filing is on with a blank folder', async () => {
    const { save } = seed({ session_folder: 'Chats' })
    await hydrated()
    fireEvent.change(screen.getByDisplayValue('Chats'), { target: { value: '   ' } })
    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ session_folder: 'Signal' })
  })

  it('sends the empty off-state when filing is switched off', async () => {
    const { save } = seed({ session_folder: 'Chats' })
    await hydrated()
    fireEvent.click(screen.getByLabelText('File sessions in a folder'))
    fireEvent.click(saveBtn())
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toMatchObject({ session_folder: '' })
  })
})

/* ── mutation arms ────────────────────────────────────────────────────────── */

describe('SignalPanel save outcomes', () => {
  it('confirms a save and keeps the edited draft rather than reseeding it', async () => {
    const { save } = seed()
    await hydrated()
    fireEvent.change(numberInput(), { target: { value: '+15550109998' } })
    fireEvent.keyDown(numberInput(), { key: 'Enter' })
    fireEvent.click(saveBtn())
    expect(await screen.findByText('Saved.', undefined, { timeout: 5_000 })).toBeInTheDocument()
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(screen.getByText('+15550109998')).toBeInTheDocument()
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

  it('renders a known error code in the UI language rather than the server prose', async () => {
    seed({}, { save: { reject: new Error(JSON.stringify({ error: 'invalid number +155***', code: 'invalid_number' })) } })
    await hydrated()
    fireEvent.click(saveBtn())
    expect(
      await screen.findByText(/neither a \+ phone number nor a Signal ACI/, undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
    expect(screen.queryByText('invalid number +155***')).not.toBeInTheDocument()
  })

  it('maps an account rejection to the account hint', async () => {
    seed({}, { save: { reject: new Error(JSON.stringify({ error: 'x', code: 'invalid_account' })) } })
    await hydrated()
    fireEvent.click(saveBtn())
    expect(
      await screen.findByText(/Enter the account number in international format/, undefined, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })

  it('unwraps an uncoded JSON error body', async () => {
    seed({}, { save: { reject: new Error(JSON.stringify({ error: 'config.json is corrupt', code: 'config_corrupt' })) } })
    await hydrated()
    fireEvent.click(saveBtn())
    expect(await screen.findByText('config.json is corrupt', undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('falls back to a plain error message when the body is not JSON', async () => {
    seed({}, { save: { reject: new Error('gateway unreachable') } })
    await hydrated()
    fireEvent.click(saveBtn())
    expect(await screen.findByText('gateway unreachable', undefined, { timeout: 5_000 })).toBeInTheDocument()
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
