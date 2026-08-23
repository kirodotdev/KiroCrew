import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { DiscordPanel } from '../pages/settings/DiscordPanel'
import { TelegramPanel } from '../pages/settings/TelegramPanel'

const mocks = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
  getTelegram: vi.fn(),
  saveTelegram: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    getDiscordConfig: mocks.getConfig,
    saveDiscordConfig: mocks.saveConfig,
    getTelegramConfig: mocks.getTelegram,
    saveTelegramConfig: mocks.saveTelegram,
  },
}))

function renderPanel(panel = <DiscordPanel />) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        {panel}
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/**
 * The three snowflake tag editors share one placeholder, so a slot is addressed
 * by its render order: 0 allowed users, 1 allowed threads, 2 shared channels.
 */
const THREADS = 1
const CHANNELS = 2

function addId(slot: number, value: string) {
  fireEvent.change(screen.getAllByPlaceholderText('123456789012345678')[slot], {
    target: { value },
  })
  fireEvent.click(screen.getAllByRole('button', { name: /add/i })[slot])
}

const save = () => fireEvent.click(screen.getByRole('button', { name: 'Save Discord settings' }))

/**
 * The fields a bot-channel GET always returns, carrying NO optional block. Spread
 * it to describe a fuller endpoint; use it bare to describe one that has never
 * persisted an optional field, which is what the default-value tests turn on.
 */
const BASE_CONFIG = {
  connected: false,
  connect_error: '',
  configured: true,
  read_only: false,
  bot_token_set: true,
  bot_token_preview: 'abc…xyz',
  enabled: true,
  allowed_user_ids: ['111111111111111111'],
  allowed_thread_ids: [],
  soft_threshold_pct: 80,
}

const REACTIONS = 'Phase reactions'
const THINKING = 'Show thinking'
const toggle = (name: string) => screen.getByRole('switch', { name })

describe('DiscordPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getConfig.mockResolvedValue({
      ...BASE_CONFIG,
      allowed_channel_ids: [],
      auto_thread: true,
      reactions_enabled: true,
      show_thinking: false,
    })
    mocks.saveConfig.mockResolvedValue({
      ok: true,
      restart_required: true,
      verify_warning: '',
    })
  })

  it('renders and saves the optional thread allow-list with its disclosure', async () => {
    renderPanel()

    expect(await screen.findByText('Allowed server thread IDs')).toBeInTheDocument()
    expect(screen.getByText(/Discord delivers content from every server channel/)).toBeInTheDocument()

    addId(THREADS, '222222222222222222')
    save()

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        allowed_user_ids: ['111111111111111111'],
        allowed_thread_ids: ['222222222222222222'],
      }))
    })
  })

  it('renders the shared-channel allow-list, its warning, and the auto-thread toggle', async () => {
    renderPanel()

    expect(await screen.findByText('Shared channels')).toBeInTheDocument()
    expect(screen.getByText('Allowed server channel IDs')).toBeInTheDocument()
    // The disclosure warning must name what widens, not a softer paraphrase.
    expect(screen.getByText(/wider disclosure boundary than a thread/)).toBeInTheDocument()
    expect(screen.getByText(/what the agent writes and what its tools print/))
      .toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Answer in a new thread' }))
      .toHaveAttribute('aria-checked', 'true')
  })

  it('sends allowed_channel_ids and auto_thread in the save payload', async () => {
    renderPanel()

    await screen.findByText('Allowed server channel IDs')
    addId(CHANNELS, '333333333333333333')
    save()

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        allowed_channel_ids: ['333333333333333333'],
        auto_thread: true,
      }))
    })
  })

  it('sends auto_thread false once turned off, and warns the listed channels are inert', async () => {
    renderPanel()

    await screen.findByText('Allowed server channel IDs')
    addId(CHANNELS, '333333333333333333')
    fireEvent.click(screen.getByRole('switch', { name: 'Answer in a new thread' }))

    expect(screen.getByText(/messages in the channels above are ignored/)).toBeInTheDocument()
    save()

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        allowed_channel_ids: ['333333333333333333'],
        auto_thread: false,
      }))
    })
  })

  it('defaults auto_thread on when the endpoint omits it', async () => {
    // A config.json that never carried the field reads as ON, matching the
    // backend default, not as an opt-out the user never made.
    mocks.getConfig.mockResolvedValue(BASE_CONFIG)
    renderPanel()

    expect(await screen.findByRole('switch', { name: 'Answer in a new thread' }))
      .toHaveAttribute('aria-checked', 'true')
  })

  it('rejects a non-numeric channel ID client-side and never sends it', async () => {
    renderPanel()

    await screen.findByText('Allowed server channel IDs')
    addId(CHANNELS, 'general')

    expect(screen.getByText('"general" is not a valid ID')).toBeInTheDocument()
    save()

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        allowed_channel_ids: [],
      }))
    })
  })

  it('renders both progress-display toggles with Discord\'s own mechanism in the copy', async () => {
    renderPanel()

    expect(await screen.findByRole('switch', { name: REACTIONS }))
      .toHaveAttribute('aria-checked', 'true')
    expect(toggle(THINKING)).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText(/queued → thinking → coding → done/)).toBeInTheDocument()
    // Discord posts a subtext note, not Slack's separate reasoning reply.
    expect(screen.getByText(/subtext note above the answer/)).toBeInTheDocument()
  })

  it('reflects a config that has already opted into thinking and out of reactions', async () => {
    mocks.getConfig.mockResolvedValue({
      ...BASE_CONFIG,
      allowed_channel_ids: [],
      auto_thread: true,
      reactions_enabled: false,
      show_thinking: true,
    })
    renderPanel()

    expect(await screen.findByRole('switch', { name: REACTIONS }))
      .toHaveAttribute('aria-checked', 'false')
    expect(toggle(THINKING)).toHaveAttribute('aria-checked', 'true')
  })

  it('sends reactions_enabled and show_thinking in the save payload', async () => {
    renderPanel()

    await screen.findByRole('switch', { name: REACTIONS })
    fireEvent.click(toggle(REACTIONS))
    fireEvent.click(toggle(THINKING))
    save()

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        reactions_enabled: false,
        show_thinking: true,
      }))
    })
  })

  it('saves reactions on when the endpoint omits the field, rather than writing back a false', async () => {
    // The whole point of the `?? true` read: an endpoint that has never
    // persisted `reactions_enabled` must not have an untouched panel save an
    // opt-out over it. `!!value` would send false here.
    mocks.getConfig.mockResolvedValue(BASE_CONFIG)
    renderPanel()

    expect(await screen.findByRole('switch', { name: REACTIONS }))
      .toHaveAttribute('aria-checked', 'true')
    expect(toggle(THINKING)).toHaveAttribute('aria-checked', 'false')
    save()

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        reactions_enabled: true,
        show_thinking: false,
      }))
    })
  })
})

describe('a channel without the shared-channel or progress-display block', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getTelegram.mockResolvedValue({
      connected: false,
      connect_error: '',
      configured: true,
      read_only: false,
      bot_token_set: true,
      bot_token_preview: 'abc…xyz',
      enabled: true,
      allowed_user_ids: ['123456789'],
      soft_threshold_pct: 80,
    })
    mocks.saveTelegram.mockResolvedValue({ ok: true, restart_required: false, verify_warning: '' })
  })

  it('renders no shared-channel controls and sends neither new field', async () => {
    renderPanel(<TelegramPanel />)

    await screen.findByRole('button', { name: 'Save Telegram settings' })
    expect(screen.queryByText('Shared channels')).not.toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Answer in a new thread' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save Telegram settings' }))

    await waitFor(() => expect(mocks.saveTelegram).toHaveBeenCalled())
    const payload = mocks.saveTelegram.mock.calls[0][0]
    expect(payload).not.toHaveProperty('allowed_channel_ids')
    expect(payload).not.toHaveProperty('auto_thread')
  })

  it('renders no progress-display controls and sends neither new field', async () => {
    renderPanel(<TelegramPanel />)

    await screen.findByRole('button', { name: 'Save Telegram settings' })
    expect(screen.queryByRole('switch', { name: REACTIONS })).not.toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: THINKING })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save Telegram settings' }))

    await waitFor(() => expect(mocks.saveTelegram).toHaveBeenCalled())
    const payload = mocks.saveTelegram.mock.calls[0][0]
    expect(payload).not.toHaveProperty('reactions_enabled')
    expect(payload).not.toHaveProperty('show_thinking')
  })
})
