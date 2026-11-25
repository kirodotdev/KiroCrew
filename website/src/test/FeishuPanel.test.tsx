import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { FeishuPanel } from '../pages/settings/FeishuPanel'
import { WeComPanel } from '../pages/settings/WeComPanel'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { scoreSettingEntry } from '../components/commandPalette/settingsSearchCore'
import { i18nT } from '../i18n/t'
import { toApiError } from '../api/apiError'

const mocks = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
  installSdk: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    getFeishuConfig: mocks.getConfig,
    saveFeishuConfig: mocks.saveConfig,
    installFeishuSdk: mocks.installSdk,
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

  it('is found by settings search, on rows this panel draws', async () => {
    renderPanel()
    await screen.findByText('Feishu app ID')
    const cases: [id: string, query: string][] = [
      ['channels.feishu-app-secret', 'app secret'],
      ['channels.feishu-app-id', 'app id'],
      ['channels.allowed-user-ids-feishu', 'allowed user ids feishu'],
      ['channels.soft-context-threshold-feishu', 'soft context threshold feishu'],
      ['channels.file-sessions-in-a-folder-feishu', 'file sessions feishu'],
    ]
    for (const [id, query] of cases) {
      const entry = SETTINGS_REGISTRY.find(e => e.id === id)
      expect(entry, `${id} missing from the registry`).toMatchObject({ tab: 'channels', params: { channel: 'feishu' } })
      expect(scoreSettingEntry(query, entry!)).not.toBeNull()
      expect(document.querySelectorAll(`[data-setting-label="${CSS.escape(i18nT(entry!.labelKey!))}"]`)).toHaveLength(1)
    }
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
      screen.getByText("Then restart Kiro Crew's background service and turn Feishu on."),
    ).toBeInTheDocument()
  })

  it('explains the in-app install in plain words, in the order to follow', async () => {
    mocks.getConfig.mockResolvedValue({
      ...base, sdk_installed: false, sdk_install_supported: true, sdk_install_command: CMD, sdk_install_in_app: true,
    })
    renderPanel()
    await screen.findByTestId('sdk-install-button')
    expect(screen.getByText(
      'Feishu needs an add-on (about 120 MB) to connect. Install it, restart, then turn Feishu on. It stays installed when the app updates.',
    )).toBeInTheDocument()
    // The body already carries the order, so the terminal path's line is not repeated.
    expect(screen.queryByText("Then restart Kiro Crew's background service and turn Feishu on.")).not.toBeInTheDocument()
  })

  it('after an in-app install, says it is installed and that a restart is due', async () => {
    // The next read reports the SDK importable, but the running gateway loaded
    // without it: the card must stay to say so instead of vanishing.
    mocks.getConfig.mockResolvedValueOnce({
      ...base, sdk_installed: false, sdk_install_supported: true, sdk_install_command: CMD, sdk_install_in_app: true,
    })
    mocks.getConfig.mockResolvedValue({
      ...base, sdk_installed: true, sdk_install_supported: true, sdk_install_command: '', sdk_install_in_app: true,
    })
    mocks.installSdk.mockResolvedValue({ ok: true, restart_required: true })
    renderPanel()
    fireEvent.click(await screen.findByTestId('sdk-install-button'))
    expect(await screen.findByTestId('sdk-installed-restart')).toHaveTextContent(
      "Installed. Next, restart Kiro Crew's background service. It takes a few seconds and briefly disconnects open chats. Then turn on “Enable Feishu” below and save.",
    )
    await waitFor(() => expect(mocks.getConfig).toHaveBeenCalledTimes(2))
    expect(screen.getByTestId('sdk-installed-restart')).toBeInTheDocument()
    expect(screen.queryByTestId('sdk-install-button')).not.toBeInTheDocument()
    // No app bridge (a browser tab, a gateway the app did not start): no button.
    expect(screen.queryByTestId('sdk-restart-gateway')).not.toBeInTheDocument()
  })

  describe('with the desktop app restart bridge', () => {
    type Win = { gatewayRestartAPI?: unknown }
    afterEach(() => { delete (window as unknown as Win).gatewayRestartAPI })

    it('offers the app\'s Restart gateway beside the installed line, behind a confirm click', async () => {
      const bridge = { available: vi.fn(async () => true), restart: vi.fn(async () => 'restarting') }
      ;(window as unknown as Win).gatewayRestartAPI = bridge
      mocks.getConfig.mockResolvedValueOnce({
        ...base, sdk_installed: false, sdk_install_supported: true, sdk_install_command: CMD, sdk_install_in_app: true,
      })
      mocks.getConfig.mockResolvedValue({
        ...base, sdk_installed: true, sdk_install_supported: true, sdk_install_command: '', sdk_install_in_app: true,
      })
      mocks.installSdk.mockResolvedValue({ ok: true, restart_required: true })
      renderPanel()
      fireEvent.click(await screen.findByTestId('sdk-install-button'))
      await screen.findByTestId('sdk-installed-restart')
      const button = await screen.findByTestId('sdk-restart-gateway')
      expect(button).toHaveTextContent(i18nT('pages.settings.aboutPanel.restart_gateway'))
      fireEvent.click(button)
      expect(bridge.restart).not.toHaveBeenCalled()
      fireEvent.click(screen.getByTestId('sdk-restart-gateway'))
      await waitFor(() => expect(bridge.restart).toHaveBeenCalledTimes(1))
      await waitFor(() => expect(screen.getByTestId('sdk-restart-gateway')).toBeDisabled())
      expect(screen.getByTestId('sdk-restart-gateway')).toHaveTextContent(i18nT('pages.settings.aboutPanel.restarting'))
    })

    it('says so when the app cannot restart, and keeps the button', async () => {
      const bridge = { available: vi.fn(async () => true), restart: vi.fn(async () => 'unavailable') }
      ;(window as unknown as Win).gatewayRestartAPI = bridge
      mocks.getConfig.mockResolvedValueOnce({
        ...base, sdk_installed: false, sdk_install_supported: true, sdk_install_command: CMD, sdk_install_in_app: true,
      })
      mocks.getConfig.mockResolvedValue({
        ...base, sdk_installed: true, sdk_install_supported: true, sdk_install_command: '', sdk_install_in_app: true,
      })
      mocks.installSdk.mockResolvedValue({ ok: true, restart_required: true })
      renderPanel()
      fireEvent.click(await screen.findByTestId('sdk-install-button'))
      fireEvent.click(await screen.findByTestId('sdk-restart-gateway'))
      fireEvent.click(screen.getByTestId('sdk-restart-gateway'))
      expect(await screen.findByTestId('sdk-restart-error')).toHaveTextContent(
        i18nT('pages.settings.aboutPanel.desktop_restart_unavailable'),
      )
      expect(screen.getByTestId('sdk-restart-gateway')).toBeEnabled()
    })

    async function installWithBridge() {
      const bridge = { available: vi.fn(async () => true), restart: vi.fn(async () => 'restarting') }
      ;(window as unknown as Win).gatewayRestartAPI = bridge
      mocks.getConfig.mockResolvedValueOnce({
        ...base, sdk_installed: false, sdk_install_supported: true, sdk_install_command: CMD, sdk_install_in_app: true,
      })
      mocks.getConfig.mockResolvedValue({
        ...base, sdk_installed: true, sdk_install_supported: true, sdk_install_command: '', sdk_install_in_app: true,
      })
      mocks.installSdk.mockResolvedValue({ ok: true, restart_required: true })
      renderPanel()
      fireEvent.click(await screen.findByTestId('sdk-install-button'))
      await screen.findByTestId('sdk-restart-gateway')
      return bridge
    }

    const SAVE_FIRST = 'Save your Feishu settings first. Restarting now would lose your unsaved changes.'

    it('holds the restart while a setting is changed but not saved, and offers it again once undone', async () => {
      const bridge = await installWithBridge()
      // Clean form: the restart is on offer and no save-first line shows.
      expect(screen.getByTestId('sdk-restart-gateway')).toBeEnabled()
      expect(screen.queryByTestId('sdk-restart-save-first')).not.toBeInTheDocument()

      const groups = screen.getByRole('switch', { name: /answer in group chats/i })
      fireEvent.click(groups)
      const button = screen.getByTestId('sdk-restart-gateway')
      expect(button).toBeDisabled()
      expect(screen.getByTestId('sdk-restart-save-first')).toHaveTextContent(SAVE_FIRST)
      expect(button).toHaveAttribute('aria-describedby', screen.getByTestId('sdk-restart-save-first').id)
      fireEvent.click(button)
      fireEvent.click(button)
      expect(bridge.restart).not.toHaveBeenCalled()

      // Back to what the server holds: nothing left to lose.
      fireEvent.click(groups)
      expect(screen.getByTestId('sdk-restart-gateway')).toBeEnabled()
      expect(screen.queryByTestId('sdk-restart-save-first')).not.toBeInTheDocument()
    })

    it('holds the restart for a typed credential until it is saved', async () => {
      const bridge = await installWithBridge()
      fireEvent.click(screen.getAllByRole('button', { name: 'Replace' })[0])
      fireEvent.change(screen.getByPlaceholderText('cli_a1b2c3d4e5f6g7h8'), { target: { value: 'cli_newappid0000' } })
      expect(screen.getByTestId('sdk-restart-gateway')).toBeDisabled()
      expect(screen.getByTestId('sdk-restart-save-first')).toHaveTextContent(SAVE_FIRST)

      fireEvent.click(screen.getByRole('button', { name: 'Save Feishu settings' }))
      await waitFor(() => expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({ bot_id: 'cli_newappid0000' })))
      await waitFor(() => expect(screen.getByTestId('sdk-restart-gateway')).toBeEnabled())
      expect(screen.queryByTestId('sdk-restart-save-first')).not.toBeInTheDocument()
      // The two-step confirm is unchanged once the hold lifts.
      fireEvent.click(screen.getByTestId('sdk-restart-gateway'))
      expect(bridge.restart).not.toHaveBeenCalled()
      fireEvent.click(screen.getByTestId('sdk-restart-gateway'))
      await waitFor(() => expect(bridge.restart).toHaveBeenCalledTimes(1))
    })

    const OTHER_OPEN_ID = 'ou_0123456789abcdef0123456789abcdef'

    it('holds the restart for an ID typed into the user allow-list but not Added, until it is cleared', async () => {
      const bridge = await installWithBridge()
      const box = screen.getByPlaceholderText(OPEN_ID)
      fireEvent.change(box, { target: { value: OTHER_OPEN_ID } })
      expect(screen.getByTestId('sdk-restart-gateway')).toBeDisabled()
      expect(screen.getByTestId('sdk-restart-save-first')).toHaveTextContent(SAVE_FIRST)
      fireEvent.click(screen.getByTestId('sdk-restart-gateway'))
      fireEvent.click(screen.getByTestId('sdk-restart-gateway'))
      expect(bridge.restart).not.toHaveBeenCalled()

      // Whitespace alone is nothing to lose.
      fireEvent.change(box, { target: { value: '   ' } })
      expect(screen.getByTestId('sdk-restart-gateway')).toBeEnabled()
      expect(screen.queryByTestId('sdk-restart-save-first')).not.toBeInTheDocument()
    })

    it('keeps the hold after Add moves the typed ID into the list, and lifts it once saved', async () => {
      await installWithBridge()
      fireEvent.change(screen.getByPlaceholderText(OPEN_ID), { target: { value: OTHER_OPEN_ID } })
      fireEvent.click(addButton('users'))
      // The text left the box but is now an unsaved list edit.
      expect(screen.getByPlaceholderText(OPEN_ID)).toHaveValue('')
      expect(screen.getByTestId('sdk-restart-gateway')).toBeDisabled()

      mocks.getConfig.mockResolvedValue({ ...installed, allowed_user_ids: [OPEN_ID, OTHER_OPEN_ID] })
      const reads = mocks.getConfig.mock.calls.length
      fireEvent.click(screen.getByRole('button', { name: 'Save Feishu settings' }))
      await waitFor(() => expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({
        allowed_user_ids: [OPEN_ID, OTHER_OPEN_ID],
      })))
      await waitFor(() => expect(mocks.getConfig.mock.calls.length).toBeGreaterThan(reads))
      await waitFor(() => expect(screen.getByTestId('sdk-restart-gateway')).toBeEnabled())
      expect(screen.queryByTestId('sdk-restart-save-first')).not.toBeInTheDocument()
    })

    it('holds the restart for an ID typed into the group allow-list but not Added', async () => {
      await installWithBridge()
      const box = screen.getByPlaceholderText(CHAT_ID)
      fireEvent.change(box, { target: { value: CHAT_ID } })
      expect(screen.getByTestId('sdk-restart-gateway')).toBeDisabled()
      expect(screen.getByTestId('sdk-restart-save-first')).toHaveTextContent(SAVE_FIRST)
      fireEvent.change(box, { target: { value: '' } })
      expect(screen.getByTestId('sdk-restart-gateway')).toBeEnabled()
    })

    it('drops a half-done confirm when an edit arrives, so the next click only arms', async () => {
      const bridge = await installWithBridge()
      fireEvent.click(screen.getByTestId('sdk-restart-gateway'))
      expect(screen.getByTestId('sdk-restart-gateway')).toHaveTextContent(i18nT('pages.settings.aboutPanel.restart_confirm'))
      const groups = screen.getByRole('switch', { name: /answer in group chats/i })
      fireEvent.click(groups)
      fireEvent.click(groups)
      expect(screen.getByTestId('sdk-restart-gateway')).toHaveTextContent(i18nT('pages.settings.aboutPanel.restart_gateway'))
      fireEvent.click(screen.getByTestId('sdk-restart-gateway'))
      expect(bridge.restart).not.toHaveBeenCalled()
    })

    // What the server reports once the SDK is installed, for a refetch after a save.
    const installed = {
      ...base, sdk_installed: true, sdk_install_supported: true, sdk_install_command: '', sdk_install_in_app: true,
    }

    it('lifts the hold after saving a threshold typed as "75.0", which the server stores as 75', async () => {
      await installWithBridge()
      fireEvent.change(screen.getByLabelText('Soft context threshold %'), { target: { value: '75.0' } })
      expect(screen.getByTestId('sdk-restart-gateway')).toBeDisabled()

      mocks.getConfig.mockResolvedValue({ ...installed, soft_threshold_pct: 75 })
      const reads = mocks.getConfig.mock.calls.length
      fireEvent.click(screen.getByRole('button', { name: 'Save Feishu settings' }))
      await waitFor(() => expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({ soft_threshold_pct: 75 })))
      await waitFor(() => expect(mocks.getConfig.mock.calls.length).toBeGreaterThan(reads))
      await waitFor(() => expect(screen.getByTestId('sdk-restart-gateway')).toBeEnabled())
      expect(screen.queryByTestId('sdk-restart-save-first')).not.toBeInTheDocument()
      // The field still reads as typed; it is the save's reading of it that matters.
      expect(screen.getByLabelText('Soft context threshold %')).toHaveValue('75.0')
    })

    it('reads "80.0" against a stored 80 as no edit at all', async () => {
      await installWithBridge()
      fireEvent.change(screen.getByLabelText('Soft context threshold %'), { target: { value: '80.0' } })
      expect(screen.getByTestId('sdk-restart-gateway')).toBeEnabled()
      expect(screen.queryByTestId('sdk-restart-save-first')).not.toBeInTheDocument()
    })

    it('lifts the hold after saving a folder name with surrounding spaces', async () => {
      await installWithBridge()
      fireEvent.click(screen.getByRole('switch', { name: /file sessions in a folder/i }))
      fireEvent.change(screen.getByLabelText('Folder name'), { target: { value: '  Lark  ' } })
      expect(screen.getByTestId('sdk-restart-gateway')).toBeDisabled()

      mocks.getConfig.mockResolvedValue({ ...installed, session_folder: 'Lark' })
      const reads = mocks.getConfig.mock.calls.length
      fireEvent.click(screen.getByRole('button', { name: 'Save Feishu settings' }))
      await waitFor(() => expect(mocks.saveConfig).toHaveBeenCalledWith(expect.objectContaining({ session_folder: 'Lark' })))
      await waitFor(() => expect(mocks.getConfig.mock.calls.length).toBeGreaterThan(reads))
      await waitFor(() => expect(screen.getByTestId('sdk-restart-gateway')).toBeEnabled())
      expect(screen.queryByTestId('sdk-restart-save-first')).not.toBeInTheDocument()
    })
  })

  it.each([
    ['sdk_download_failed', "Couldn't download the Feishu SDK. Check your internet connection, then click Install again."],
    ['sdk_install_failed', "Couldn't install the Feishu SDK. Click Install to try again. The gateway log has the details."],
  ])('words a %s failure itself in ErrorNotice, without navigating away from drafts', async (code, wording) => {
    mocks.getConfig.mockResolvedValue({
      ...base, sdk_installed: false, sdk_install_supported: true, sdk_install_command: CMD, sdk_install_in_app: true,
    })
    // The gateway's own text never reaches the card: pip's lines stay in its log.
    const message = 'ERROR: No matching distribution found for lark-oapi==1.8.0'
    mocks.installSdk.mockRejectedValue(await toApiError(new Response(
      JSON.stringify({ error: message, code }),
      { status: 502, headers: { 'Content-Type': 'application/json' } },
    )))
    renderPanel()
    fireEvent.click(await screen.findByTestId('sdk-install-button'))
    const notice = await screen.findByTestId('sdk-install-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent(wording)
    expect(notice).not.toHaveTextContent(message)
    expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
    expect(screen.queryByTestId('sdk-installed-restart')).not.toBeInTheDocument()
    // While the install is failing, the next step is Install again, not a restart.
    expect(screen.queryByText(/background service/)).not.toBeInTheDocument()
    expect(screen.getByTestId('sdk-install-button')).toBeEnabled()
  })

  it('keeps the gateway message for a refusal that has no wording of its own', async () => {
    mocks.getConfig.mockResolvedValue({
      ...base, sdk_installed: false, sdk_install_supported: true, sdk_install_command: CMD, sdk_install_in_app: true,
    })
    mocks.installSdk.mockRejectedValue(await toApiError(new Response(
      JSON.stringify({ error: 'an install is already running', code: 'install_already_running' }),
      { status: 409, headers: { 'Content-Type': 'application/json' } },
    )))
    renderPanel()
    fireEvent.click(await screen.findByTestId('sdk-install-button'))
    expect(await screen.findByTestId('sdk-install-error')).toHaveTextContent('an install is already running')
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
