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
import { screen, fireEvent, waitFor, within } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import { api, type SignalConfigData } from '../api/client'
import { SignalPanel, isSignalId, isSignalNumber, normalizeSignalId, signalOperatorMode } from '../pages/settings/SignalPanel'
import { signalListRecipientsCommand, signalReceiveCommand } from '../pages/settings/signalCliCommand'
import { CATALOGS } from '../i18n/catalogs'
import { SUPPORTED_LANGUAGES } from '../i18n/languages'

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

/** The placeholders, worded as examples so neither reads as a saved value. */
const ACCOUNT_PLACEHOLDER = 'e.g. +44 7700 900123'
const ALLOWED_PLACEHOLDER = 'e.g. +44 7700 900456'

/** The allow-list's own text input: the tag editor's, not the account field. */
function numberInput() {
  return screen.getByPlaceholderText(ALLOWED_PLACEHOLDER)
}

function operatorWarning() {
  return screen.queryByTestId('signal-operator-warning')
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

/* ── operator scope ───────────────────────────────────────────────────────── */

const BOT = OWN
const OWNER = '+447700900456'
const FRIEND = '+447700900789'
const ACI = '1b2c3d4e-0000-4000-8000-000000000001'

describe('signalOperatorMode mirrors signal_cli.transport.operator_mode', () => {
  // The same table as test_signal_config_handlers.py's _OPERATOR_CASES, which
  // checks the backend endpoint and the transport against it.
  const cases: Array<[string, string[], string]> = [
    [BOT, [], 'none'],
    [BOT, [OWNER], 'sole_entry'],
    [BOT, [OWNER, FRIEND], 'none'],
    [BOT, [OWNER, '+44 7700 900-123'], 'account'],
    [BOT, [OWNER, '0044.7700.900123'], 'account'],
    [BOT, [BOT], 'account'],
    ['', [OWNER, FRIEND], 'none'],
    ['', [OWNER], 'sole_entry'],
    [BOT, [OWNER, '+44 7700 900456'], 'sole_entry'],
  ]
  it.each(cases)('account %s with %j is %s', (account, allowed, expected) => {
    expect(signalOperatorMode(allowed, account)).toBe(expected)
  })

  it('compares an ACI case-insensitively and ignores an entry the backend drops', () => {
    expect(signalOperatorMode([OWNER, ACI.toUpperCase()], BOT)).toBe('none')
    expect(signalOperatorMode([OWNER, 'not-a-number'], BOT)).toBe('sole_entry')
  })
})

describe('SignalPanel operator warning', () => {
  it('warns when a bot number allows two senders', async () => {
    seed({ account: BOT, allowed_numbers: [OWNER, FRIEND] })
    await hydrated()
    const warning = operatorWarning()
    expect(warning).toHaveTextContent('Nobody can approve tools or use /stop')
    expect(warning).toHaveTextContent('Keep a single allowed number, or use the linked setup')
  })

  it.each([
    ['a single allowed number', { account: BOT, allowed_numbers: [OWNER] }],
    ['the account on its own list', { account: BOT, allowed_numbers: [OWNER, '+44 7700 900 123'] }],
    ['an empty list', { account: BOT, allowed_numbers: [] }],
  ])('stays quiet for %s', async (_name, over) => {
    seed(over)
    await hydrated()
    expect(operatorWarning()).not.toBeInTheDocument()
  })

  it('appears as soon as a second number is added in the draft', async () => {
    seed({ account: BOT, allowed_numbers: [OWNER] })
    await hydrated()
    expect(operatorWarning()).not.toBeInTheDocument()
    fireEvent.change(numberInput(), { target: { value: FRIEND } })
    fireEvent.keyDown(numberInput(), { key: 'Enter' })
    expect(screen.getByText(FRIEND)).toBeInTheDocument()
    expect(operatorWarning()).toBeInTheDocument()
  })

  it('takes the backend’s answer while connected and unedited', async () => {
    // The running client learned the account's ACI, which the browser cannot
    // know: the server says the account is on the list.
    seed({ connected: true, account: BOT, allowed_numbers: [OWNER, ACI], operator_mode: 'account' })
    await hydrated()
    expect(operatorWarning()).not.toBeInTheDocument()
  })

  it('takes the backend’s warning too, and the mirror once the draft is edited', async () => {
    seed({ connected: true, account: BOT, allowed_numbers: [OWNER, ACI], operator_mode: 'none' })
    await hydrated()
    expect(operatorWarning()).toBeInTheDocument()
    fireEvent.change(numberInput(), { target: { value: BOT } })
    fireEvent.keyDown(numberInput(), { key: 'Enter' })
    expect(screen.getByText(BOT, { selector: 'span' })).toBeInTheDocument()
    expect(operatorWarning()).not.toBeInTheDocument()
  })

  it('uses the mirror while the channel is not connected', async () => {
    seed({ connected: false, account: BOT, allowed_numbers: [OWNER, FRIEND], operator_mode: 'account' })
    await hydrated()
    expect(operatorWarning()).toBeInTheDocument()
  })
})

/* ── placeholders ─────────────────────────────────────────────────────────── */

describe('SignalPanel placeholders', () => {
  it('word both examples as examples, never as the saved values', async () => {
    seed({ account: '', allowed_numbers: [] })
    await hydrated()
    expect(accountInput()).toHaveAttribute('placeholder', ACCOUNT_PLACEHOLDER)
    expect(numberInput()).toHaveAttribute('placeholder', ALLOWED_PLACEHOLDER)
  })

  it.each(SUPPORTED_LANGUAGES.filter(l => !l.devOnly).map(l => l.code))(
    '%s prefixes the example with words of its own',
    (code) => {
      const translation = CATALOGS[code].translation as Record<string, Record<string, Record<string, Record<string, string>>>>
      const value = translation.pages.settings.signalPanel.example_placeholder
      expect(value).toContain('{{example}}')
      expect(value.replace('{{example}}', '').trim()).not.toBe('')
    },
  )
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

  it('says what failed, the command to check and that it retries', async () => {
    seed({ connect_error: 'User +447*** is not registered.' })
    await hydrated()
    const message = screen.getByText(/could not start for this account/)
    // One full stop after signal-cli's reason, not its own plus the sentence's.
    expect(message.textContent).toContain('signal-cli could not start for this account: User +447*** is not registered.')
    expect(message.textContent).not.toContain('registered..')
    const notice = screen.getByRole('alert')
    expect(notice).toHaveTextContent(`registered or linked: signal-cli -a ${OWN} receive should work`)
    expect(notice).toHaveTextContent('keeps retrying on its own')
  })

  it('sets the command to check in a code element, never in literal backticks', async () => {
    seed({ connect_error: 'User +447*** is not registered.' })
    await hydrated()
    const notice = screen.getByRole('alert')
    const command = within(notice).getByText(`signal-cli -a ${OWN} receive`)
    expect(command.tagName).toBe('CODE')
    expect(notice.textContent).not.toContain('`')
  })
})

/* ── notices ──────────────────────────────────────────────────────────────── */

describe('SignalPanel copy', () => {
  it('says what the channel gives you in the tagline', async () => {
    seed()
    await hydrated()
    expect(screen.getByText('Chat with your agents and get their notifications in Signal.')).toBeInTheDocument()
    // How it works moved to the setup text.
    expect(screen.getByText(/drives a local signal-cli, with no bot token and no hosted relay/)).toBeInTheDocument()
  })

  it('names an ACI in words and says where to find one', async () => {
    seed()
    await hydrated()
    expect(screen.getByText(/or Signal account IDs \(ACIs\), that may message the agent/)).toBeInTheDocument()
    const hint = screen.getByTestId('signal-aci-hint')
    expect(hint).toHaveTextContent(/A Signal account ID \(ACI\) is a UUID/)
    const command = within(hint).getByText(`signal-cli -a ${OWN} listContacts --all-recipients`)
    expect(command.tagName).toBe('CODE')
  })

  it('quotes the commands as bare shell text, the account in place', () => {
    expect(signalReceiveCommand(OWN)).toBe(`signal-cli -a ${OWN} receive`)
    expect(signalListRecipientsCommand(OWN)).toBe(`signal-cli -a ${OWN} listContacts --all-recipients`)
    // No account yet: signal-cli falls back to its only local account.
    expect(signalListRecipientsCommand('')).toBe('signal-cli listContacts --all-recipients')
  })

  it.each(SUPPORTED_LANGUAGES.filter(l => !l.devOnly).map(l => l.code))(
    '%s wraps each quoted command in the code tag and keeps the placeholder',
    (code) => {
      const translation = CATALOGS[code].translation as Record<string, Record<string, Record<string, Record<string, string>>>>
      const panel = translation.pages.settings.signalPanel
      for (const key of ['aci_hint', 'connection_failed_check']) {
        expect(panel[key]).toContain('<command>{{command}}</command>')
      }
      expect(panel.connection_failed).not.toContain('{{command}}')
    },
  )
})

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

  it('suggests listing the account for Note to Self while the allow-list is empty', async () => {
    seed({ allowed_numbers: [] })
    await hydrated()
    expect(screen.getByText(/to chat in Note to Self/)).toBeInTheDocument()
  })

  it('stays quiet for a dedicated bot number whose own number is not on the list', async () => {
    // The bot setup: the owner's number is allowed, the account's is not, and
    // that is correct, so nothing under the list may suggest otherwise.
    seed({ account: '+15550109876', allowed_numbers: ['+15550101234'] })
    await hydrated()
    expect(screen.queryByText(/to chat in Note to Self/)).not.toBeInTheDocument()
    expect(screen.queryByText(/it will answer nobody/)).not.toBeInTheDocument()
  })

  it('does not suggest it once the account is allowed', async () => {
    seed({ allowed_numbers: ['+44 7700 900123'] })
    await hydrated()
    expect(screen.queryByText(/to chat in Note to Self/)).not.toBeInTheDocument()
  })

  it('shows one line, the warning, for an enabled channel with an empty list', async () => {
    seed({ enabled: true, allowed_numbers: [] })
    await hydrated()
    expect(screen.getByText(/it will answer nobody/)).toBeInTheDocument()
    expect(screen.queryByText(/to chat in Note to Self/)).not.toBeInTheDocument()
  })

  it('drops the hint as soon as a number is added to the empty list', async () => {
    seed({ allowed_numbers: [] })
    await hydrated()
    fireEvent.change(numberInput(), { target: { value: '+15550101234' } })
    fireEvent.keyDown(numberInput(), { key: 'Enter' })
    expect(screen.getByText('+15550101234')).toBeInTheDocument()
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
      await screen.findByText(/neither a \+ phone number nor a Signal account ID \(ACI\)/, undefined, { timeout: 5_000 }),
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
