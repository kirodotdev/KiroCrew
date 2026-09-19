import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { DiscordPanel } from '../pages/settings/DiscordPanel'

const mocks = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    getDiscordConfig: mocks.getConfig,
    saveDiscordConfig: mocks.saveConfig,
  },
}))

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <DiscordPanel />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** Config as the endpoint returns it, with the folder setting off (the default). */
function config(session_folder = '') {
  return {
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
    session_folder,
  }
}

describe('per-channel session folder', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getConfig.mockResolvedValue(config())
    mocks.saveConfig.mockResolvedValue({ ok: true, restart_required: false, verify_warning: '' })
  })

  it('is off by default and hides the name field until turned on', async () => {
    renderPanel()

    const toggle = await screen.findByRole('switch', { name: 'File sessions in a folder' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(screen.queryByText('Folder name')).not.toBeInTheDocument()

    fireEvent.click(toggle)
    expect(await screen.findByText('Folder name')).toBeInTheDocument()
  })

  it('sends the channel name when turned on with a blank name', async () => {
    renderPanel()

    fireEvent.click(await screen.findByRole('switch', { name: 'File sessions in a folder' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save Discord settings' }))

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(
        expect.objectContaining({ session_folder: 'Discord' }),
      )
    })
  })

  it('sends a custom folder name as typed', async () => {
    renderPanel()

    fireEvent.click(await screen.findByRole('switch', { name: 'File sessions in a folder' }))
    const nameInput = await screen.findByPlaceholderText('Discord')
    fireEvent.change(nameInput, { target: { value: '  Team chat  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Discord settings' }))

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(
        expect.objectContaining({ session_folder: 'Team chat' }),
      )
    })
  })

  it('reflects a configured folder and clears it to "" when turned off', async () => {
    mocks.getConfig.mockResolvedValue(config('Team chat'))
    renderPanel()

    const toggle = await screen.findByRole('switch', { name: 'File sessions in a folder' })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByDisplayValue('Team chat')).toBeInTheDocument()

    fireEvent.click(toggle)
    fireEvent.click(screen.getByRole('button', { name: 'Save Discord settings' }))

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(
        expect.objectContaining({ session_folder: '' }),
      )
    })
  })

  it('tolerates a config payload without the field (older gateway)', async () => {
    const { session_folder: _omitted, ...withoutField } = config()
    mocks.getConfig.mockResolvedValue(withoutField)
    renderPanel()

    const toggle = await screen.findByRole('switch', { name: 'File sessions in a folder' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
  })
})

describe('filing existing conversations (#2661)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.saveConfig.mockResolvedValue({ ok: true, restart_required: false, verify_warning: '' })
  })

  it('offers the button once a folder is configured', async () => {
    mocks.getConfig.mockResolvedValue(config('Team chat'))
    renderPanel()

    expect(
      await screen.findByRole('button', { name: /File existing sessions/i }),
    ).toBeInTheDocument()
  })

  it('does not offer it while the setting is off', async () => {
    mocks.getConfig.mockResolvedValue(config())
    renderPanel()

    await screen.findByRole('switch', { name: 'File sessions in a folder' })
    expect(
      screen.queryByRole('button', { name: /File existing sessions/i }),
    ).not.toBeInTheDocument()
  })

  it('does not appear from the DRAFT toggle alone', async () => {
    // The endpoint acts on PERSISTED config, so a button offered for a folder
    // that has not been saved yet would answer "not configured" on every click
    // and read as broken. Gated on the server's value for exactly that reason.
    mocks.getConfig.mockResolvedValue(config())
    renderPanel()

    fireEvent.click(await screen.findByRole('switch', { name: 'File sessions in a folder' }))
    expect(await screen.findByText('Folder name')).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /File existing sessions/i }),
    ).not.toBeInTheDocument()
  })

  it('names the channel the backend keys on, not the display name', async () => {
    // BotChannelPanel serves four channels, so the namespace has to arrive from
    // the spec. Sending the display name would reach no config section at all.
    mocks.getConfig.mockResolvedValue(config('Team chat'))
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          folder_name: 'Team chat',
          moved: [],
          reason: '',
          remaining: 0,
          failed: 0,
        }),
    })
    vi.stubGlobal('fetch', fetchMock)
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: /File existing sessions/i }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ namespace: 'discord' })
    vi.unstubAllGlobals()
  })
})
