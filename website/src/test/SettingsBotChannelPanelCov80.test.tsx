// BotChannelPanel — the shared bot-token channel panel. The forum and
// session-folder suites cover those two optional blocks; this one covers the
// rest: load/failure placeholders, the status pill, every connection hint, the
// optional second credential / allow-all / thread allow-list blocks, the
// threshold guard, the secret-clear paths and the save result messages.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import {
  BotChannelPanel,
  type BotChannelSpec,
  type BotChannelConfigData,
} from '../pages/settings/BotChannelPanel'

function config(overrides: Partial<BotChannelConfigData> = {}): BotChannelConfigData {
  return {
    connected: false,
    connect_error: '',
    configured: true,
    read_only: false,
    bot_token_set: true,
    bot_token_preview: 'zz11…zz99',
    enabled: true,
    allowed_user_ids: ['42'],
    soft_threshold_pct: 80,
    ...overrides,
  }
}

let seq = 0

function makeSpec(overrides: Partial<BotChannelSpec> = {}): BotChannelSpec {
  return {
    name: 'Zzchat',
    queryKey: `cov80-bot-${seq++}`,
    logo: <span data-testid="brand-logo" />,
    description: 'zz-description',
    host: 'zz.invalid',
    setupGuide: 'https://example.invalid/guide',
    guideBody: 'zz-guide-body',
    guideLink: { label: 'zz-open-console', href: 'https://example.invalid' },
    tokenDescription: 'zz-token-desc',
    tokenPlaceholder: 'zz-token-placeholder',
    allowlistDescription: 'zz-allowlist-desc',
    allowlistPlaceholder: '123',
    thresholdDescription: 'zz-threshold-desc',
    emptyAllowlistHint: 'zz-empty-allowlist-hint',
    getConfig: () => Promise.resolve(config()),
    saveConfig: vi.fn().mockResolvedValue({ ok: true, restart_required: false, verify_warning: '' }),
    ...overrides,
  }
}

function renderPanel(spec: BotChannelSpec) {
  renderWithProviders(<BotChannelPanel spec={spec} />)
  return spec
}

const saveBtn = () => screen.findByRole('button', { name: /Save Zzchat settings/i })

beforeEach(() => { seq += 1 })
afterEach(() => { vi.useRealTimers() })

describe('BotChannelPanel load states', () => {
  it('shows a channel-named placeholder while loading', () => {
    renderPanel(makeSpec({ getConfig: () => new Promise(() => {}) }))
    expect(screen.getByText(/Loading/)).toBeInTheDocument()
    expect(screen.getByText(/Zzchat/)).toBeInTheDocument()
  })

  it('explains a load failure', async () => {
    renderPanel(makeSpec({ getConfig: () => Promise.reject(new Error('down')) }))
    expect(await screen.findByText(/Cannot load/)).toBeInTheDocument()
  })

  it('hides the save control and shows a notice in a read-only session', async () => {
    renderPanel(makeSpec({ getConfig: () => Promise.resolve(config({ read_only: true })) }))
    expect((await screen.findAllByText(/read-only from remote sessions/i)).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /Save Zzchat settings/i })).not.toBeInTheDocument()
  })
})

describe('BotChannelPanel status and hints', () => {
  it('reads Connected when the channel is live', async () => {
    renderPanel(makeSpec({ getConfig: () => Promise.resolve(config({ connected: true })) }))
    expect(await screen.findByText('Connected')).toBeInTheDocument()
  })

  it('reads Needs setup before anything is configured', async () => {
    renderPanel(makeSpec({
      getConfig: () => Promise.resolve(config({ configured: false, bot_token_set: false, enabled: false })),
    }))
    expect(await screen.findByText('Needs setup')).toBeInTheDocument()
  })

  it('names the host and the error when the channel failed to start', async () => {
    renderPanel(makeSpec({ getConfig: () => Promise.resolve(config({ connect_error: 'zz-401' })) }))
    const hint = await screen.findByText(/zz-401/)
    expect(hint.textContent).toContain('zz.invalid')
  })

  it('asks for a restart when configuration is saved but nothing is running', async () => {
    renderPanel(makeSpec())
    expect(await screen.findByText(/Configuration is saved but the channel is not running/)).toBeInTheDocument()
  })

  it('warns that an empty allow-list fails closed', async () => {
    renderPanel(makeSpec({
      getConfig: () => Promise.resolve(config({ configured: false, allowed_user_ids: [] })),
    }))
    expect(await screen.findByText('zz-empty-allowlist-hint')).toBeInTheDocument()
  })

  it('stays quiet about the allow-list when allow-all is on', async () => {
    renderPanel(makeSpec({
      allowAll: { label: 'zz-allow-all', description: 'zz-allow-all-desc', bypassNote: 'zz-bypass-note' },
      getConfig: () => Promise.resolve(config({
        configured: false, allowed_user_ids: [], allow_all_users: true,
      })),
    }))
    expect(await screen.findByText('zz-allow-all')).toBeInTheDocument()
    expect(screen.queryByText('zz-empty-allowlist-hint')).not.toBeInTheDocument()
    expect(screen.getByText('zz-bypass-note')).toBeInTheDocument()
  })
})

describe('BotChannelPanel optional blocks', () => {
  it('renders a second credential above the token when the spec asks for one', async () => {
    renderPanel(makeSpec({
      secondCredential: { label: 'Zz bot ID', description: 'zz-id-desc', placeholder: 'zz-id' },
      getConfig: () => Promise.resolve(config({ bot_id_set: false, bot_id_preview: '' })),
    }))
    expect(await screen.findByLabelText('Zz bot ID')).toBeInTheDocument()
  })

  it('sends the second credential only when it was entered', async () => {
    const saveConfig = vi.fn().mockResolvedValue({ ok: true, restart_required: false, verify_warning: '' })
    renderPanel(makeSpec({
      saveConfig,
      secondCredential: { label: 'Zz bot ID', description: 'zz-id-desc', placeholder: 'zz-id' },
      getConfig: () => Promise.resolve(config({ bot_id_set: false })),
    }))
    fireEvent.change(await screen.findByLabelText('Zz bot ID'), { target: { value: ' zz-id-value ' } })
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    await waitFor(() => expect(saveConfig).toHaveBeenCalled())
    expect(saveConfig.mock.calls[0][0].bot_id).toBe('zz-id-value')
  })

  it('renders the thread allow-list with its help and warning', async () => {
    renderPanel(makeSpec({
      threadAllowlist: {
        label: 'zz-threads', description: 'zz-threads-desc', placeholder: '99',
        help: <>zz-threads-help</>, warning: <>zz-threads-warning</>,
      },
      getConfig: () => Promise.resolve(config({ allowed_thread_ids: ['77'] })),
    }))
    expect(await screen.findByText('zz-threads')).toBeInTheDocument()
    expect(screen.getByText('zz-threads-help')).toBeInTheDocument()
    expect(screen.getByText('zz-threads-warning')).toBeInTheDocument()
  })

  it('sends thread ids only when the spec has that block', async () => {
    const saveConfig = vi.fn().mockResolvedValue({ ok: true, restart_required: false, verify_warning: '' })
    renderPanel(makeSpec({
      saveConfig,
      threadAllowlist: {
        label: 'zz-threads', description: 'zz-threads-desc', placeholder: '99',
        help: <>zz-threads-help</>, warning: <>zz-threads-warning</>,
      },
      getConfig: () => Promise.resolve(config({ allowed_thread_ids: ['77'] })),
    }))
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    await waitFor(() => expect(saveConfig).toHaveBeenCalled())
    expect(saveConfig.mock.calls[0][0].allowed_thread_ids).toEqual(['77'])
  })

  it('uses a channel-supplied allow-list validator over the numeric default', async () => {
    const allowlistValidate = vi.fn().mockReturnValue(true)
    renderPanel(makeSpec({ allowlistValidate }))
    await screen.findByText('Allowed user IDs')
    const entry = screen.getByPlaceholderText('123')
    fireEvent.change(entry, { target: { value: 'zz.user@corp' } })
    fireEvent.keyDown(entry, { key: 'Enter' })
    expect(allowlistValidate).toHaveBeenCalledWith('zz.user@corp')
  })

  it('overrides the guide and token labels when the spec supplies them', async () => {
    renderPanel(makeSpec({ guideTitle: 'zz-guide-title', tokenLabel: 'zz-token-label' }))
    expect(await screen.findByText('zz-guide-title')).toBeInTheDocument()
    expect(screen.getByText('zz-token-label')).toBeInTheDocument()
    expect(screen.queryByText('Get your bot token')).not.toBeInTheDocument()
  })
})

describe('BotChannelPanel save', () => {
  it('refuses a threshold outside 1-100 without calling the API', async () => {
    const saveConfig = vi.fn()
    renderPanel(makeSpec({ saveConfig }))
    await screen.findByText('Allowed user IDs')
    fireEvent.change(screen.getByPlaceholderText('80'), { target: { value: '0' } })
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    expect(await screen.findByText(/must be a number between 1 and 100/)).toBeInTheDocument()
    expect(saveConfig).not.toHaveBeenCalled()
  })

  it('refuses a non-numeric threshold', async () => {
    const saveConfig = vi.fn()
    renderPanel(makeSpec({ saveConfig }))
    await screen.findByText('Allowed user IDs')
    fireEvent.change(screen.getByPlaceholderText('80'), { target: { value: 'zz' } })
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    expect(await screen.findByText(/must be a number between 1 and 100/)).toBeInTheDocument()
    expect(saveConfig).not.toHaveBeenCalled()
  })

  it('marks a stored token for removal instead of sending a blank one', async () => {
    const saveConfig = vi.fn().mockResolvedValue({ ok: true, restart_required: false, verify_warning: '' })
    renderPanel(makeSpec({ saveConfig }))
    fireEvent.click(await screen.findByLabelText('Remove'))
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    await waitFor(() => expect(saveConfig).toHaveBeenCalled())
    expect(saveConfig.mock.calls[0][0]).toMatchObject({ bot_token_clear: true })
    expect(saveConfig.mock.calls[0][0]).not.toHaveProperty('bot_token')
  })

  it('confirms verification when a freshly pasted token checked out', async () => {
    const saveConfig = vi.fn().mockResolvedValue({ ok: true, restart_required: true, verify_warning: '' })
    renderPanel(makeSpec({ saveConfig, getConfig: () => Promise.resolve(config({ bot_token_set: false })) }))
    fireEvent.change(await screen.findByLabelText('Zzchat bot token'), { target: { value: ' zz-token ' } })
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    await waitFor(() => expect(screen.getByText(/Verified with Zzchat and saved/)).toBeInTheDocument())
    expect(saveConfig.mock.calls[0][0].bot_token).toBe('zz-token')
  })

  it('shows the verification warning alongside the save confirmation', async () => {
    const saveConfig = vi.fn().mockResolvedValue({ ok: true, restart_required: true, verify_warning: 'zz-verify-warning' })
    renderPanel(makeSpec({ saveConfig, getConfig: () => Promise.resolve(config({ bot_token_set: false })) }))
    fireEvent.change(await screen.findByLabelText('Zzchat bot token'), { target: { value: 'zz-token' } })
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    await waitFor(() => expect(screen.getByText('zz-verify-warning')).toBeInTheDocument())
    expect(screen.getByText(/Restart the gateway to apply/)).toBeInTheDocument()
  })

  it('confirms a plain save', async () => {
    renderPanel(makeSpec())
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    await waitFor(() => expect(screen.getByText('Saved.')).toBeInTheDocument())
  })

  it('unwraps a JSON error body from a failed save', async () => {
    const saveConfig = vi.fn().mockRejectedValue(new Error(JSON.stringify({ error: 'zz-json-reason' })))
    renderPanel(makeSpec({ saveConfig }))
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    await waitFor(() => expect(screen.getByText('zz-json-reason')).toBeInTheDocument())
  })

  it('falls back to the raw message, then to a generic one', async () => {
    const saveConfig = vi.fn().mockRejectedValue(new Error('zz-plain-reason'))
    renderPanel(makeSpec({ saveConfig }))
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    await waitFor(() => expect(screen.getByText('zz-plain-reason')).toBeInTheDocument())
  })

  it('falls back to a generic message when the rejection carries none', async () => {
    const saveConfig = vi.fn().mockRejectedValue({ nope: true })
    renderPanel(makeSpec({ saveConfig }))
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    await waitFor(() => expect(screen.getByText(/Save failed/)).toBeInTheDocument())
  })

  it('clears the save confirmation on its timer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    renderPanel(makeSpec())
    const btn = await saveBtn()
    await act(async () => { fireEvent.click(btn) })
    await waitFor(() => expect(screen.getByText('Saved.')).toBeInTheDocument())
    await act(async () => { vi.advanceTimersByTime(6500) })
    expect(screen.queryByText('Saved.')).not.toBeInTheDocument()
  })
})
