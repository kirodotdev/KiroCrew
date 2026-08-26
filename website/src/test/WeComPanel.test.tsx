import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { WeComPanel } from '../pages/settings/WeComPanel'

const mocks = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    getWeComConfig: mocks.getConfig,
    saveWeComConfig: mocks.saveConfig,
  },
}))

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <WeComPanel />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('WeComPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getConfig.mockResolvedValue({
      connected: false,
      connect_error: '',
      configured: true,
      read_only: false,
      bot_token_set: true,
      bot_token_preview: 'AbC…89cd',
      bot_id_set: true,
      bot_id_preview: 'wxb…cdef',
      enabled: true,
      allowed_user_ids: ['zhangsan'],
      allow_all_users: false,
      soft_threshold_pct: 80,
    })
    mocks.saveConfig.mockResolvedValue({
      ok: true,
      restart_required: true,
      verify_warning: '',
    })
  })

  it('renders both credential fields (bot ID + secret)', async () => {
    renderPanel()
    expect(await screen.findByText('WeCom bot ID')).toBeInTheDocument()
    expect(screen.getByText('WeCom bot secret')).toBeInTheDocument()
    // Both are already set: their masked previews render.
    expect(screen.getByText(/wxb…cdef/)).toBeInTheDocument()
    expect(screen.getByText(/AbC…89cd/)).toBeInTheDocument()
  })

  it('accepts alphanumeric WeCom userids in the allow-list and saves both credentials', async () => {
    renderPanel()

    // Add a non-numeric userid — the WeCom validator must accept it (the
    // numeric default used by Telegram/Discord would reject it).
    const idInput = await screen.findByPlaceholderText('zhangsan')
    fireEvent.change(idInput, { target: { value: 'li.si-01@corp' } })
    fireEvent.click(screen.getByRole('button', { name: /add/i }))

    // Replace both credentials (stored secrets show a masked preview until
    // the Replace affordance reveals the input).
    const replaceButtons = screen.getAllByRole('button', { name: 'Replace' })
    expect(replaceButtons).toHaveLength(2)
    replaceButtons.forEach(btn => fireEvent.click(btn))
    fireEvent.change(screen.getByPlaceholderText('Paste WeCom bot ID'), {
      target: { value: 'wxb-new-id' },
    })
    fireEvent.change(screen.getByPlaceholderText('Paste WeCom bot secret'), {
      target: { value: 'new-secret-value' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save WeCom settings' }))

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        allowed_user_ids: ['zhangsan', 'li.si-01@corp'],
        bot_id: 'wxb-new-id',
        bot_token: 'new-secret-value',
      }))
    })
  })

  it('rejects userids with whitespace in the tag editor', async () => {
    renderPanel()
    const idInput = await screen.findByPlaceholderText('zhangsan')
    fireEvent.change(idInput, { target: { value: 'zhang san' } })
    fireEvent.click(screen.getByRole('button', { name: /add/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Save WeCom settings' }))
    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        // The invalid entry never entered the list.
        allowed_user_ids: ['zhangsan'],
      }))
    })
  })

  it('sends the explicit allow-all opt-in and shows the bypass note', async () => {
    renderPanel()
    const toggle = await screen.findByRole('switch', { name: /allow all organization members/i })
    fireEvent.click(toggle)
    // Turning it on surfaces the bypass note under the allow-list.
    expect(await screen.findByText(/userid list above is bypassed/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save WeCom settings' }))
    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        allow_all_users: true,
      }))
    })
  })
})
