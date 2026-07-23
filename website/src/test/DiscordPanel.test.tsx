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

describe('DiscordPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getConfig.mockResolvedValue({
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

    const idInputs = screen.getAllByPlaceholderText('123456789012345678')
    fireEvent.change(idInputs[1], { target: { value: '222222222222222222' } })
    fireEvent.click(screen.getAllByRole('button', { name: /add/i })[1])
    fireEvent.click(screen.getByRole('button', { name: 'Save Discord settings' }))

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        allowed_user_ids: ['111111111111111111'],
        allowed_thread_ids: ['222222222222222222'],
      }))
    })
  })
})
