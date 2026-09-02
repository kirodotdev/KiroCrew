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
