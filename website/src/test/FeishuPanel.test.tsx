import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { FeishuPanel } from '../pages/settings/FeishuPanel'
import { WeComPanel } from '../pages/settings/WeComPanel'

const mocks = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    getFeishuConfig: mocks.getConfig,
    saveFeishuConfig: mocks.saveConfig,
    // The last case renders WeComPanel to prove the group section is opt-in;
    // it reads the same mocks, so its own shape is set per test.
    getWeComConfig: mocks.getConfig,
    saveWeComConfig: mocks.saveConfig,
  },
}))

/**
 * The Add button for one of the panel's tag editors.
 *
 * Two are rendered once `groupChats` is present — users first, then groups — so
 * `getByRole('button', { name: /add/i })` is ambiguous. Position is the honest
 * discriminator here: both buttons carry identical accessible names, and the
 * order is the render order the spec fixes.
 */
function addButton(which: 'users' | 'groups'): HTMLElement {
  const buttons = screen.getAllByRole('button', { name: /add/i })
  expect(buttons).toHaveLength(2)
  return buttons[which === 'users' ? 0 : 1]
}

const OPEN_ID = 'ou_c99cbd8a1b2c3d4e5f6a7b8c9d0e1f2a'
const CHAT_ID = 'oc_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6'

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <FeishuPanel />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('FeishuPanel', () => {
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
      bot_id_preview: 'cli…6g7h',
      enabled: true,
      allowed_user_ids: [OPEN_ID],
      allow_group: false,
      allowed_group_ids: [],
      soft_threshold_pct: 80,
    })
    mocks.saveConfig.mockResolvedValue({
      ok: true,
      restart_required: true,
      verify_warning: '',
    })
  })

  it('renders both credential fields (app ID + app secret)', async () => {
    renderPanel()
    expect(await screen.findByText('Feishu app ID')).toBeInTheDocument()
    expect(screen.getByText('Feishu app secret')).toBeInTheDocument()
    // Both are already set: their masked previews render, never a raw value.
    expect(screen.getByText(/cli…6g7h/)).toBeInTheDocument()
    expect(screen.getByText(/AbC…89cd/)).toBeInTheDocument()
  })

  it('accepts an ou_ open_id in the allow-list and saves both credentials', async () => {
    renderPanel()

    const idInput = await screen.findByPlaceholderText(OPEN_ID)
    fireEvent.change(idInput, { target: { value: 'ou_0011aabbccdd2233' } })
    fireEvent.click(addButton('users'))

    // Stored secrets show a masked preview until Replace reveals the input.
    const replaceButtons = screen.getAllByRole('button', { name: 'Replace' })
    expect(replaceButtons).toHaveLength(2)
    replaceButtons.forEach(btn => fireEvent.click(btn))
    fireEvent.change(screen.getByPlaceholderText('cli_a1b2c3d4e5f6g7h8'), {
      target: { value: 'cli_newappid0000' },
    })
    fireEvent.change(screen.getByPlaceholderText('Paste Feishu app secret'), {
      target: { value: 'new-app-secret-value' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save Feishu settings' }))

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        allowed_user_ids: [OPEN_ID, 'ou_0011aabbccdd2233'],
        bot_id: 'cli_newappid0000',
        bot_token: 'new-app-secret-value',
      }))
    })
  })

  it('rejects an id without the ou_ prefix, matching the backend validator', async () => {
    renderPanel()
    const idInput = await screen.findByPlaceholderText(OPEN_ID)
    // A chat_id pasted into the USER list is the likely mistake, and the two
    // lists are not interchangeable — the transport reads them for different
    // decisions, so the wrong one here would silently authorise nobody.
    fireEvent.change(idInput, { target: { value: CHAT_ID } })
    fireEvent.click(addButton('users'))
    fireEvent.click(screen.getByRole('button', { name: 'Save Feishu settings' }))
    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        // The invalid entry never entered the list.
        allowed_user_ids: [OPEN_ID],
      }))
    })
  })

  it('sends the group-chat opt-in and warns while its allow-list is empty', async () => {
    renderPanel()
    const toggle = await screen.findByRole('switch', { name: /answer in group chats/i })
    fireEvent.click(toggle)
    // Both fail closed, so "on with an empty list" serves no group at all —
    // the panel has to say so rather than look configured.
    expect(await screen.findByText(/turn the toggle off/i)).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText(CHAT_ID), { target: { value: CHAT_ID } })
    fireEvent.click(addButton('groups'))
    fireEvent.click(screen.getByRole('button', { name: 'Save Feishu settings' }))

    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        allow_group: true,
        allowed_group_ids: [CHAT_ID],
      }))
    })
  })

  it('surfaces the missing lark-oapi extra as the reason the channel is down', async () => {
    mocks.getConfig.mockResolvedValue({
      connected: false,
      connect_error: "lark-oapi is not installed — run: pip install 'lark-oapi>=1.4,<2'",
      configured: true,
      read_only: false,
      bot_token_set: true,
      bot_token_preview: 'AbC…89cd',
      bot_id_set: true,
      bot_id_preview: 'cli…6g7h',
      enabled: true,
      allowed_user_ids: [OPEN_ID],
      allow_group: false,
      allowed_group_ids: [],
      soft_threshold_pct: 80,
    })
    renderPanel()
    expect(await screen.findByText(/lark-oapi is not installed/)).toBeInTheDocument()
  })

  it('omits the group section fields for a channel that does not declare it', async () => {
    // Guard on the shared panel rather than on Feishu: `groupChats` is opt-in,
    // so a channel without it must not start sending allow_group and silently
    // widen its own config. WeCom is that channel.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mocks.getConfig.mockResolvedValue({
      connected: false,
      connect_error: '',
      configured: true,
      read_only: false,
      bot_token_set: false,
      bot_token_preview: '',
      bot_id_set: false,
      bot_id_preview: '',
      enabled: true,
      allowed_user_ids: ['zhangsan'],
      allow_all_users: false,
      soft_threshold_pct: 80,
    })
    render(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <WeComPanel />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    expect(await screen.findByRole('button', { name: 'Save WeCom settings' })).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: /answer in group chats/i })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Save WeCom settings' }))
    await waitFor(() => {
      expect(mocks.saveConfig).toHaveBeenCalled()
    })
    const payload = mocks.saveConfig.mock.calls[0][0]
    expect(payload).not.toHaveProperty('allow_group')
    expect(payload).not.toHaveProperty('allowed_group_ids')
  })
})

/**
 * lark-oapi ships as the optional [feishu] extra, so a fully credentialed channel
 * still cannot start without it. The card is the only surface that says so before
 * a restart -- the connection badge cannot, because `maybe_start_feishu` returns
 * at its first line when the channel is disabled and the ImportError branch that
 * records the missing SDK sits after that return.
 */
describe('FeishuPanel missing-SDK card', () => {
  const CMD = "/opt/venv/bin/python -m pip install 'lark-oapi>=1.4,<2'"

  const base = {
    connected: false, connect_error: '', configured: true, read_only: false,
    bot_token_set: true, bot_token_preview: 'AbC…89cd',
    bot_id_set: true, bot_id_preview: 'cli…6g7h',
    enabled: true, allowed_user_ids: [OPEN_ID],
    allow_group: false, allowed_group_ids: [], soft_threshold_pct: 80,
  }

  beforeEach(() => {
    vi.clearAllMocks()
    mocks.saveConfig.mockResolvedValue({ ok: true, restart_required: true, verify_warning: '' })
  })

  it('shows the command naming the gateway interpreter when the SDK is missing', async () => {
    mocks.getConfig.mockResolvedValue({
      ...base, sdk_installed: false, sdk_install_supported: true, sdk_install_command: CMD,
    })
    renderPanel()
    // The full command must be present verbatim: a truncated or re-wrapped copy
    // would defeat the one thing this card exists to get right.
    expect(await screen.findByText(CMD)).toBeInTheDocument()
    // The card's own restart line, matched exactly: the connection hint
    // ("Configuration is saved but the channel is not running. Restart the
    // gateway to connect.") also mentions a restart, so a loose /restart the
    // gateway/ would pass on that instead of on this card.
    expect(
      screen.getByText('Then restart the gateway to start the Feishu channel.'),
    ).toBeInTheDocument()
  })

  it('stays hidden once the SDK is importable', async () => {
    mocks.getConfig.mockResolvedValue({
      ...base, sdk_installed: true, sdk_install_supported: true, sdk_install_command: '',
    })
    renderPanel()
    await screen.findByText('Feishu')
    expect(screen.queryByText(/Install the Feishu SDK/i)).not.toBeInTheDocument()
  })

  it('stays hidden against a gateway that does not report the field', async () => {
    // Strictly `=== false`: an older gateway omits it, and treating undefined as
    // "missing" would tell every user of one to install a package they may have.
    mocks.getConfig.mockResolvedValue({ ...base })
    renderPanel()
    await screen.findByText('Feishu')
    expect(screen.queryByText(/Install the Feishu SDK/i)).not.toBeInTheDocument()
  })

  it('offers no command where a pip install cannot work', async () => {
    mocks.getConfig.mockResolvedValue({
      ...base, sdk_installed: false, sdk_install_supported: false, sdk_install_command: '',
    })
    renderPanel()
    expect(await screen.findByText(/cannot install extra packages/i)).toBeInTheDocument()
    expect(screen.queryByText(CMD)).not.toBeInTheDocument()
  })

  it('renders no card for a channel that declares no SDK extra', async () => {
    // WeCom's client is in core, so its spec omits `sdkExtra` entirely and the
    // shared panel must not grow a card from the shared fields alone.
    mocks.getConfig.mockResolvedValue({
      ...base, sdk_installed: false, sdk_install_supported: true, sdk_install_command: CMD,
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <WeComPanel />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    await screen.findByText('WeCom')
    expect(screen.queryByText(CMD)).not.toBeInTheDocument()
  })
})
