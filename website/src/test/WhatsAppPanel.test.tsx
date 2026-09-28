import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { WhatsAppPanel } from '../pages/settings/WhatsAppPanel'
import { ApiError, api, type WhatsAppConfigData } from '../api/client'

/**
 * WhatsApp channel panel.
 *
 * The panel's whole job is to communicate STATE (paired, pairing, connected) and
 * to gate who may MESSAGE the agent, so the cases below are lifecycle states and
 * the access policy rather than layout. The policy admits a sender to chat and
 * nothing more: `transport_dispatch` denies every tool to a non-operator, so the
 * label must not promise command authority this control cannot grant.
 *
 * The Playwright harness (`scripts/capture-whatsapp-panel.mjs`) photographs the
 * same states against a built `dist/`; this keeps them under the unit suite,
 * which is what the coverage gate reads.
 *
 * Three groups carry invariants that a screenshot cannot:
 *
 * - **The badge must distinguish three states.** `configured` is computed by the
 *   gateway as `enabled AND connected`, so a panel branching on it collapses
 *   "paired but currently down" into "connected" and the operator is told the
 *   channel is fine while it carries no traffic. The cases below pin all three.
 * - **Pairing is revealed, not started.** No dashboard call can begin pairing, so
 *   the control must be inert with an explanation rather than appear to offer it.
 * - **Unlink has three distinguishable outcomes**, two of which leave work to do.
 *   A 502 means the device is STILL LINKED; reporting it as done leaves a live
 *   device with full read and send access on the operator's account.
 *
 * The policy picker is a Radix Select, so a `change` event on the trigger does
 * nothing: open it, then click the option.
 */

/** Comfortably past `useImeGuard`'s 50ms post-`compositionend` latch window, so
 *  the follow-up Enter below is a real submit rather than a still-latched one. */
const POST_COMPOSITION_GRACE_MS = 80

const CONFIG: WhatsAppConfigData = {
  configured: true,
  connected: true,
  connect_error: '',
  read_only: false,
  enabled: true,
  dm_policy: 'self',
  allowed_wa_ids: [],
  groups: [],
  session_folder: '',
  state: 'connected',
}

const QR_IDLE = { state: 'connected', qr_data_url: null, detail: '' }

const seed = (patch: Partial<WhatsAppConfigData>) =>
  vi.spyOn(api, 'getWhatsAppConfig').mockResolvedValue({ ...CONFIG, ...patch })

/** The shape `j()` throws for a non-2xx JSON body, so `code` survives to the UI. */
const apiError = (status: number, code: string, message = 'backend text') =>
  new ApiError(status, message, JSON.stringify({ error: message, code }))

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'getWhatsAppConfig').mockResolvedValue({ ...CONFIG })
  vi.spyOn(api, 'whatsAppQrStatus').mockResolvedValue({ ...QR_IDLE })
  vi.spyOn(api, 'getWhatsAppGroups').mockResolvedValue({ groups: [] })
})

describe('WhatsAppPanel — status', () => {
  it('reports a connected channel', async () => {
    renderWithProviders(<WhatsAppPanel />)
    expect(await screen.findByTestId('whatsapp-panel')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Connected')).toBeInTheDocument())
  })

  it('distinguishes never-paired from paired-but-down', async () => {
    // `configured` is true in both fixtures, it is `enabled AND connected`
    // server-side and cannot carry this distinction, so the badge must read
    // `state`.
    seed({ connected: false, state: 'unpaired' })
    const { unmount } = renderWithProviders(<WhatsAppPanel />)
    await waitFor(() => expect(screen.getByText('Not paired')).toBeInTheDocument())
    expect(screen.queryByText('Paired, but not connected')).toBeNull()
    unmount()

    seed({ connected: false, state: 'error' })
    renderWithProviders(<WhatsAppPanel />)
    await waitFor(() =>
      expect(screen.getByText('Paired, but not connected')).toBeInTheDocument(),
    )
    expect(screen.queryByText('Not paired')).toBeNull()
    expect(screen.queryByText('Connected')).toBeNull()
  })

  it('names a revoked link rather than calling it unpaired', async () => {
    seed({ connected: false, state: 'logged_out' })
    renderWithProviders(<WhatsAppPanel />)
    await waitFor(() => expect(screen.getByText('The link was revoked')).toBeInTheDocument())
  })

  it('renders WHY the channel is down, not just that it is', async () => {
    seed({ connected: false, state: 'unpaired', connect_error: 'error: dial tcp timeout' })
    renderWithProviders(<WhatsAppPanel />)
    await waitFor(() =>
      expect(screen.getByTestId('whatsapp-connect-error')).toHaveTextContent(
        'error: dial tcp timeout',
      ),
    )
  })

  it('says nothing about a failure when the channel is up', async () => {
    renderWithProviders(<WhatsAppPanel />)
    expect(await screen.findByTestId('whatsapp-panel')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Connected')).toBeInTheDocument())
    expect(screen.queryByTestId('whatsapp-connect-error')).toBeNull()
  })

  it('explains itself when the config cannot be read', async () => {
    vi.spyOn(api, 'getWhatsAppConfig').mockRejectedValue(new Error('gateway down'))
    renderWithProviders(<WhatsAppPanel />)
    // The panel must say something rather than render an empty pane.
    await waitFor(() => expect(document.body.textContent).toBeTruthy())
  })
})

describe('WhatsAppPanel — who can message the agent', () => {
  it('shows the saved policy on the trigger', async () => {
    renderWithProviders(<WhatsAppPanel />)
    const trigger = await screen.findByRole('combobox', { name: 'Who can message the agent' })
    await waitFor(() => expect(trigger).toHaveTextContent('Only me (my own messages)'))
  })

  it('lists every policy in the popup', async () => {
    renderWithProviders(<WhatsAppPanel />)
    fireEvent.click(await screen.findByRole('combobox', { name: 'Who can message the agent' }))
    const options = (await screen.findAllByRole('option')).map(o => o.textContent)
    expect(options).toContain('Only me (my own messages)')
    expect(options).toContain('Me, plus allowed numbers')
    expect(options).toContain('Nobody (ignore all messages)')
  })

  it('selecting a policy PUTs the new value', async () => {
    const save = vi
      .spyOn(api, 'saveWhatsAppConfig')
      .mockResolvedValue({ ok: true, restart_required: true })
    renderWithProviders(<WhatsAppPanel />)
    fireEvent.click(await screen.findByRole('combobox', { name: 'Who can message the agent' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Me, plus allowed numbers' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith({ dm_policy: 'allowlist' }))
  })

  it('reveals the number list only for the allowlist policy', async () => {
    seed({ dm_policy: 'allowlist', allowed_wa_ids: ['447700900111'] })
    renderWithProviders(<WhatsAppPanel />)
    await waitFor(() => expect(screen.getByText('447700900111')).toBeInTheDocument())
  })

  it('disables the picker when the config is read-only', async () => {
    seed({ read_only: true })
    renderWithProviders(<WhatsAppPanel />)
    const trigger = await screen.findByRole('combobox', { name: 'Who can message the agent' })
    await waitFor(() => expect(trigger).toBeDisabled())
  })
})

describe('WhatsAppPanel — enable toggle', () => {
  it('persists the enable switch', async () => {
    const save = vi
      .spyOn(api, 'saveWhatsAppConfig')
      .mockResolvedValue({ ok: true, restart_required: true })
    seed({ enabled: false })
    renderWithProviders(<WhatsAppPanel />)
    const toggle = await screen.findByRole('switch', { name: /Enable the WhatsApp channel/ })
    fireEvent.click(toggle)
    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ enabled: true })))
  })
})

describe('WhatsAppPanel — pairing', () => {
  it('renders the QR image while a code is live', async () => {
    seed({ connected: false, state: 'pairing' })
    vi.spyOn(api, 'whatsAppQrStatus').mockResolvedValue({
      state: 'pairing',
      qr_data_url: 'data:image/png;base64,AAAA',
      detail: '',
    })
    vi.spyOn(api, 'whatsAppQrStart').mockResolvedValue({ ok: true, state: 'pairing' })
    renderWithProviders(<WhatsAppPanel />)
    // The card is gated on the panel's own phase, which only leaves 'idle' when
    // the operator asks for the code; polling the status endpoint is not enough.
    const button = await screen.findByRole('button', { name: /Show pairing code/ })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => {
      const img = document.querySelector('img[src^="data:image/png"]')
      expect(img).not.toBeNull()
    })
  })

  it('does not offer a code the gateway cannot produce', async () => {
    // Pairing is started by the channel's own connect(), so with no live code
    // there is nothing a click could do. The panel must say so instead.
    const start = vi.spyOn(api, 'whatsAppQrStart').mockResolvedValue({ ok: true })
    seed({ connected: false, state: 'unpaired' })
    renderWithProviders(<WhatsAppPanel />)
    const button = await screen.findByRole('button', { name: /Show pairing code/ })
    await waitFor(() => expect(button).toBeDisabled())
    expect(screen.getByTestId('whatsapp-pairing-unavailable')).toHaveTextContent(
      /pairing begins when the channel starts/i,
    )
    fireEvent.click(button)
    expect(start).not.toHaveBeenCalled()
  })

  it('tells the operator to unlink first when a device is already paired', async () => {
    renderWithProviders(<WhatsAppPanel />)
    await waitFor(() =>
      expect(screen.getByTestId('whatsapp-pairing-unavailable')).toHaveTextContent(
        /Already paired/i,
      ),
    )
  })

  it('never leaves a spinner up when the live state has no code', async () => {
    // The cached config read can be a poll interval stale, so the probe's answer
    // is the authority: a non-pairing state must abandon the wait.
    seed({ connected: false, state: 'pairing' })
    vi.spyOn(api, 'whatsAppQrStart').mockResolvedValue({ ok: true, state: 'logged_out' })
    renderWithProviders(<WhatsAppPanel />)
    const button = await screen.findByRole('button', { name: /Show pairing code/ })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() =>
      expect(screen.getByTestId('whatsapp-pairing-unavailable')).toHaveTextContent(
        /Restart the gateway to pair a device again/i,
      ),
    )
    expect(screen.queryByTestId('whatsapp-qr')).toBeNull()
  })

  it('translates a not-running channel instead of echoing the backend', async () => {
    seed({ connected: false, state: 'pairing' })
    vi.spyOn(api, 'whatsAppQrStart').mockRejectedValue(
      apiError(409, 'channel_not_running', 'channel not running (enable whatsapp and restart)'),
    )
    renderWithProviders(<WhatsAppPanel />)
    const button = await screen.findByRole('button', { name: /Show pairing code/ })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() =>
      expect(screen.getByTestId('whatsapp-error')).toHaveTextContent(
        'The WhatsApp channel is not running. Enable it below, then restart the gateway.',
      ),
    )
  })
})

describe('WhatsAppPanel: unlink', () => {
  it('does not revoke on a single click', async () => {
    const unlink = vi.spyOn(api, 'whatsAppUnlink').mockResolvedValue({ ok: true })
    renderWithProviders(<WhatsAppPanel />)
    fireEvent.click(await screen.findByTestId('whatsapp-unlink-button'))
    // The credential has no second factor and no expiry, so the first click only
    // arms: the label is what announces the confirmation step.
    await waitFor(() =>
      expect(screen.getByTestId('whatsapp-unlink-button')).toHaveTextContent('Confirm unlink'),
    )
    expect(unlink).not.toHaveBeenCalled()
  })

  it('revokes on the confirming click and says what to do next', async () => {
    const unlink = vi.spyOn(api, 'whatsAppUnlink').mockResolvedValue({ ok: true })
    renderWithProviders(<WhatsAppPanel />)
    const button = await screen.findByTestId('whatsapp-unlink-button')
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(unlink).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(screen.getByTestId('whatsapp-unlink-outcome')).toHaveTextContent(
        'Unlinked. Restart the gateway to pair a device again.',
      ),
    )
  })

  it('reports a leftover session file as unlinked-with-a-file, not as clean', async () => {
    vi.spyOn(api, 'whatsAppUnlink').mockResolvedValue({
      ok: true,
      warning: 'unlinked, but the local session file could not be removed',
      code: 'session_file_kept',
    })
    renderWithProviders(<WhatsAppPanel />)
    const button = await screen.findByTestId('whatsapp-unlink-button')
    fireEvent.click(button)
    fireEvent.click(button)
    const outcome = await screen.findByTestId('whatsapp-unlink-outcome')
    await waitFor(() => expect(outcome).toHaveTextContent(/local session file could not be removed/i))
    // The file holds the linked-device keys, so this must not read as a plain
    // unlink.
    expect(outcome).not.toHaveTextContent('Unlinked. Restart the gateway to pair a device again.')
  })

  it('never reports a refused logout as a revoke', async () => {
    vi.spyOn(api, 'whatsAppUnlink').mockRejectedValue(
      apiError(502, 'logout_failed', 'could not unlink from WhatsApp; the device is still linked'),
    )
    renderWithProviders(<WhatsAppPanel />)
    const button = await screen.findByTestId('whatsapp-unlink-button')
    fireEvent.click(button)
    fireEvent.click(button)
    const outcome = await screen.findByTestId('whatsapp-unlink-outcome')
    await waitFor(() => expect(outcome).toHaveTextContent(/the device is still linked/i))
    expect(outcome).toHaveAttribute('role', 'alert')
    expect(outcome).not.toHaveTextContent(/^Unlinked\./)
  })

  it('is hidden from a remote session, which the backend refuses anyway', async () => {
    seed({ read_only: true })
    renderWithProviders(<WhatsAppPanel />)
    expect(await screen.findByTestId('whatsapp-panel')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('whatsapp-unlink')).toBeNull())
  })
})

describe('WhatsAppPanel: groups', () => {
  const GROUP = {
    jid: '120363000000000001@g.us',
    name: 'Weekend plans',
    mode: 'mention' as const,
    rules: '',
    cooldown_s: 120,
  }

  it('lists the opted-in groups with their JIDs', async () => {
    seed({ groups: [GROUP] })
    renderWithProviders(<WhatsAppPanel />)
    await waitFor(() => expect(screen.getByText('Weekend plans')).toBeInTheDocument())
    expect(screen.getByText('120363000000000001@g.us')).toBeInTheDocument()
  })

  it('adds a joined group at the quietest mode', async () => {
    const save = vi
      .spyOn(api, 'saveWhatsAppConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    vi.spyOn(api, 'getWhatsAppGroups').mockResolvedValue({
      groups: [{ jid: GROUP.jid, name: GROUP.name }],
    })
    renderWithProviders(<WhatsAppPanel />)
    fireEvent.click(await screen.findByRole('combobox', { name: 'Add a group' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Weekend plans' }))
    // `mention` never speaks unprompted, so adding a group cannot by itself put
    // the agent into a conversation.
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith({
        groups: [{ jid: GROUP.jid, name: GROUP.name, mode: 'mention', rules: '', cooldown_s: 120 }],
      }),
    )
  })

  it('reveals the rules field only for rules mode and commits it on blur', async () => {
    const save = vi
      .spyOn(api, 'saveWhatsAppConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    seed({ groups: [{ ...GROUP, mode: 'rules' }] })
    renderWithProviders(<WhatsAppPanel />)
    const field = await screen.findByLabelText('When the agent may speak')
    fireEvent.change(field, { target: { value: 'Answer 3D printing questions' } })
    fireEvent.blur(field)
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith({
        groups: [{ ...GROUP, mode: 'rules', rules: 'Answer 3D printing questions' }],
      }),
    )
  })

  it('keeps the rules field out of the way in mention mode', async () => {
    seed({ groups: [GROUP] })
    renderWithProviders(<WhatsAppPanel />)
    await waitFor(() => expect(screen.getByText('Weekend plans')).toBeInTheDocument())
    expect(screen.queryByLabelText('When the agent may speak')).toBeNull()
  })

  it('removes a group from the stored list', async () => {
    const save = vi
      .spyOn(api, 'saveWhatsAppConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    seed({ groups: [GROUP] })
    renderWithProviders(<WhatsAppPanel />)
    fireEvent.click(await screen.findByTestId(`whatsapp-group-remove-${GROUP.jid}`))
    await waitFor(() => expect(save).toHaveBeenCalledWith({ groups: [] }))
  })

  it('explains an empty picker while the channel is down', async () => {
    seed({ connected: false, state: 'unpaired' })
    renderWithProviders(<WhatsAppPanel />)
    await waitFor(() =>
      expect(screen.getByTestId('whatsapp-group-picker')).toHaveTextContent(
        'Your groups appear here once the channel is connected.',
      ),
    )
  })
})

describe('WhatsAppPanel — session folder', () => {
  it('files sessions into a folder when enabled', async () => {
    const save = vi
      .spyOn(api, 'saveWhatsAppConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    renderWithProviders(<WhatsAppPanel />)
    const toggle = await screen.findByRole('switch', { name: /File sessions in a folder/ })
    fireEvent.click(toggle)
    // Enabling persists the default (brand) name, never an empty string.
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(expect.objectContaining({ session_folder: 'WhatsApp' })),
    )
  })

  it('shows the saved folder name', async () => {
    seed({ session_folder: 'Phone' })
    renderWithProviders(<WhatsAppPanel />)
    await waitFor(() => expect(screen.getByDisplayValue('Phone')).toBeInTheDocument())
  })

  it('surfaces a rejected folder name instead of keeping it silently', async () => {
    seed({ session_folder: 'Phone' })
    vi.spyOn(api, 'saveWhatsAppConfig').mockRejectedValue(new Error('invalid folder name'))
    renderWithProviders(<WhatsAppPanel />)
    const input = await screen.findByDisplayValue('Phone')
    fireEvent.change(input, { target: { value: 'bad/name' } })
    fireEvent.blur(input)
    await waitFor(() =>
      expect(screen.getByTestId('whatsapp-session-folder-error')).toHaveTextContent(
        'invalid folder name',
      ),
    )
  })

  it('does not commit the folder name on an IME candidate-committing Enter', async () => {
    // A CJK operator's Enter ACCEPTS the candidate; it is not a submit. The text
    // in the field at that moment is intermediate, so committing it persists a
    // half-composed name. On WebKit the commit keydown arrives AFTER
    // `compositionend` with `isComposing` already false, so the hook's 50ms latch
    // is the only signal that survives into it — which is why the field has to
    // carry `bindComposition`, not just read the native flag.
    const save = vi
      .spyOn(api, 'saveWhatsAppConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    seed({ session_folder: 'Phone' })
    renderWithProviders(<WhatsAppPanel />)
    const input = (await screen.findByDisplayValue('Phone')) as HTMLInputElement
    // Really focused, because the commit runs through `blur()` and a `blur()` on
    // an unfocused element fires no event — the assertion would hold for the
    // wrong reason.
    input.focus()
    fireEvent.change(input, { target: { value: '手机' } })
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    fireEvent.keyDown(input, { key: 'Enter' })
    // The MECHANISM first: the guard's job is to not blur, and blur is what
    // commits. Asserting only "did not save" would pass vacuously here — the
    // save is a mutation, so at this instant it has not been issued either way.
    expect(document.activeElement).toBe(input)
    // Then the outcome, once the mutation would have landed. The same wait takes
    // us past the hook's post-composition window, which the contrast below needs:
    // inside it, a real Enter is indistinguishable from the candidate commit
    // above, and that indistinguishability is the whole point of the latch.
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, POST_COMPOSITION_GRACE_MS))
    })
    expect(save).not.toHaveBeenCalled()
    // The deliberate contrast: a real Enter still commits, so the guard cannot be
    // satisfied by a field that has simply stopped saving.
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(expect.objectContaining({ session_folder: '手机' })),
    )
  })
})

describe('WhatsAppPanel — group edits compose', () => {
  it('composes a second group edit on the first, not on the server snapshot', async () => {
    // This panel saves on change and each edit rebuilds the WHOLE list, so two
    // edits issued before the refetch lands would both read the pre-edit
    // `data.groups`, and the second write would silently discard the first.
    const saved: Array<Record<string, unknown>> = []
    let release: (() => void) | null = null
    seed({
      groups: [
        { jid: 'a@g.us', name: 'A', mode: 'mention', rules: '', cooldown_s: 120 },
        { jid: 'b@g.us', name: 'B', mode: 'mention', rules: '', cooldown_s: 120 },
      ],
    })
    vi.spyOn(api, 'saveWhatsAppConfig').mockImplementation(async patch => {
      saved.push(patch as Record<string, unknown>)
      // Held open so the second edit is issued while the first is still in
      // flight, which is the whole point: a resolved save would have refetched.
      await new Promise<void>(r => {
        release = r
      })
      return { ok: true, restart_required: true }
    })

    renderWithProviders(<WhatsAppPanel />)
    // The mode control is a searchable combobox: open it, then pick the option.
    const rowA = await screen.findByTestId('whatsapp-group-a@g.us')
    fireEvent.click(within(rowA).getByRole('combobox', { name: 'How the agent joins in' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Also when its rules apply' }))
    await waitFor(() => expect(saved.length).toBe(1))

    const rowB = await screen.findByTestId('whatsapp-group-b@g.us')
    fireEvent.click(within(rowB).getByRole('combobox', { name: 'How the agent joins in' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Off, keeping the entry' }))
    await waitFor(() => expect(saved.length).toBe(2))
    release?.()

    const second = saved[1].groups as Array<{ jid: string; mode: string }>
    expect(second.find(g => g.jid === 'a@g.us')?.mode).toBe('rules')
    expect(second.find(g => g.jid === 'b@g.us')?.mode).toBe('off')
  })
})
