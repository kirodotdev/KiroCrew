/**
 * The Dev Container trust prompt is gated on the FEATURE, not just the file.
 *
 * `agent.devcontainer` defaults to `off`, and a repo ships `devcontainer.json`
 * regardless — so `has_config` alone made the card ask for a decision that could
 * not change anything, which is the usability defect this pins. `enabled` on the
 * status response is the config mode, and it gates both the prompt and the
 * composer's running-container chip.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'

const devcontainerStatus = vi.fn()
const devcontainerTrust = vi.fn().mockResolvedValue({ trusted: true, digest: 'abcdef0123456789' })
const devcontainerUntrust = vi.fn().mockResolvedValue({ trusted: false, removed: true })

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    projectGit: vi.fn().mockResolvedValue({ branch: 'main', detached: false, head: '' }),
    devcontainerStatus: (project: string) => devcontainerStatus(project),
    devcontainerTrust: (project: string, digest: string) => devcontainerTrust(project, digest),
    devcontainerUntrust: (project: string) => devcontainerUntrust(project),
    devcontainerConfig: vi.fn().mockResolvedValue({
      config_path: '/repo/.devcontainer/devcontainer.json',
      digest: 'abcdef0123456789',
      raw: '{"name": "repo"}',
      name: 'repo',
      image: null,
      trusted: false,
    }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

const PROJECT = '/home/u/work/repo'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: 0, running: false, stop_state: 'idle', mode: '', project: PROJECT, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as ReturnType<typeof dashboardReducer>,
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as ReturnType<typeof chatReducer>,
      notifications: { items: [] } as unknown as ReturnType<typeof notificationsReducer>,
    },
  })
}

function renderChat() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const result = render(
    <Provider store={makeStore()}>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <ChatPage />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return { ...result, queryClient }
}

/**
 * Drive one status poll. The real page polls on an interval and on window
 * focus; invalidating is the same refetch of the same active query, without
 * making the test depend on timers.
 */
async function pollStatus(queryClient: QueryClient) {
  await act(async () => {
    await queryClient.invalidateQueries({ queryKey: ['devcontainer-status', PROJECT] })
  })
}

const status = (over: Record<string, unknown> = {}) => ({
  project_dir: PROJECT,
  enabled: true,
  has_config: true,
  config_path: `${PROJECT}/.devcontainer/devcontainer.json`,
  trusted: false,
  config_digest: 'digest-of-the-original-bytes',
  container_id: null,
  running: false,
  remote_workspace_folder: null,
  ...over,
})

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('ChatPage Dev Container trust prompt', () => {
  it('prompts when the feature is on and the config is untrusted', async () => {
    devcontainerStatus.mockResolvedValue(status())
    renderChat()
    await waitFor(() => expect(devcontainerStatus).toHaveBeenCalledWith(PROJECT))
    await waitFor(() =>
      expect(screen.getByText(/run this project's agent inside its dev container\?/i)).toBeInTheDocument(),
    )
  })

  it('sends the reviewed digest with the grant, not just the project', async () => {
    // The security property end to end: the digest the card displayed is what
    // reaches the API, so a config rewritten between the preview and the click
    // cannot be authorized (the backend refuses a mismatch with 409).
    devcontainerStatus.mockResolvedValue(status())
    renderChat()
    const button = await screen.findByRole('button', { name: /^trust$/i })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(devcontainerTrust).toHaveBeenCalledTimes(1))
    expect(devcontainerTrust).toHaveBeenCalledWith(PROJECT, 'abcdef0123456789')
  })

  it('never prompts while the feature is off, config or not', async () => {
    devcontainerStatus.mockResolvedValue(status({ enabled: false }))
    renderChat()
    await waitFor(() => expect(devcontainerStatus).toHaveBeenCalledWith(PROJECT))
    // Settle the render the status resolution triggers before asserting absence,
    // so this cannot pass merely because the query had not landed yet.
    await waitFor(() => expect(screen.queryByText(/dev container/i)).not.toBeInTheDocument())
  })

  it('hides the running-container chip while the feature is off', async () => {
    devcontainerStatus.mockResolvedValue(
      status({ enabled: false, trusted: true, running: true, container_id: 'aabbccddeeff0011' }),
    )
    renderChat()
    await waitFor(() => expect(devcontainerStatus).toHaveBeenCalledWith(PROJECT))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /dev container/i })).not.toBeInTheDocument(),
    )
  })

  it('shows the chip when the feature is on and a container runs', async () => {
    devcontainerStatus.mockResolvedValue(
      status({ trusted: true, running: true, container_id: 'aabbccddeeff0011' }),
    )
    renderChat()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /dev container/i })).toBeInTheDocument(),
    )
  })

  it('keeps the card up to confirm the grant instead of unmounting silently', async () => {
    // The status flips to trusted the moment the grant lands, but nothing
    // observable happens until the next session spawns. Unmounting on `trusted`
    // left the user with no evidence the click did anything.
    devcontainerStatus.mockResolvedValueOnce(status()).mockResolvedValue(status({ trusted: true }))
    renderChat()
    const button = await screen.findByRole('button', { name: /^trust$/i })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)

    await waitFor(() => expect(devcontainerTrust).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByText('Dev Container trusted')).toBeInTheDocument())
    expect(
      screen.getByText(/the next session for this project starts in the dev container/i),
    ).toBeInTheDocument()

    // Acknowledging it is what removes the card, and it does not come back.
    fireEvent.click(screen.getByRole('button', { name: /got it/i }))
    await waitFor(() => expect(screen.queryByText('Dev Container trusted')).not.toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /^trust$/i })).not.toBeInTheDocument()
  })

  it('does not re-ask for trust immediately after it was withdrawn', async () => {
    // Revoking only deletes the grant: the container and the session keep
    // running, so `trusted` goes back to false while the chip stays. Without a
    // dismissal that re-mounted the trust card on top of an explicit "no" — and
    // claimed the chat was running on this machine while it was containerized.
    devcontainerStatus
      .mockResolvedValueOnce(status({ trusted: true, running: true, container_id: 'aabbccddeeff' }))
      .mockResolvedValue(status({ trusted: false, running: true, container_id: 'aabbccddeeff' }))
    renderChat()
    const chip = await screen.findByRole('button', { name: 'Dev Container' })
    // No prompt while trust is granted.
    expect(screen.queryByRole('button', { name: /^trust$/i })).not.toBeInTheDocument()

    fireEvent.click(chip)
    fireEvent.click(screen.getByRole('menuitem', { name: /withdraw trust/i }))

    await waitFor(() => expect(devcontainerUntrust).toHaveBeenCalledWith(PROJECT))
    await waitFor(() => expect(devcontainerStatus).toHaveBeenCalledTimes(2))
    // The untrusted status has landed and the prompt still does not return.
    await waitFor(() =>
      expect(screen.queryByText(/run this project's agent inside its dev container\?/i))
        .not.toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /^trust$/i })).not.toBeInTheDocument()
  })

  it('re-prompts when an edit invalidates trust after the grant was acknowledged', async () => {
    // Trust is bound to the config's digest, so editing `devcontainer.json`
    // invalidates it and the poll reports untrusted again. Acknowledging the
    // grant confirmation must therefore NOT record a dismissal: the dismissal
    // key is the config PATH, which an edit does not change, so it would
    // suppress this prompt for the rest of the page's life.
    devcontainerStatus
      .mockResolvedValueOnce(status())
      .mockResolvedValueOnce(status({ trusted: true }))
      .mockResolvedValue(status({ trusted: false }))
    const { queryClient } = renderChat()

    const button = await screen.findByRole('button', { name: /^trust$/i })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByText('Dev Container trusted')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /got it/i }))
    await waitFor(() => expect(screen.queryByText('Dev Container trusted')).not.toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /^trust$/i })).not.toBeInTheDocument()

    // The edit landed: the next poll reports the grant no longer matches.
    await pollStatus(queryClient)
    await waitFor(() =>
      expect(screen.getByText(/run this project's agent inside its dev container\?/i)).toBeInTheDocument(),
    )
    expect(await screen.findByRole('button', { name: /^trust$/i })).toBeInTheDocument()
  })

  it('acknowledges the grant even when the status refetch behind it fails', async () => {
    // The status query does not retry, so a refetch that fails leaves the
    // pre-grant (untrusted) payload cached. The grant call itself returning is
    // what proves trust is held, so acknowledging must take the card down
    // rather than fall back to the prompt for a decision already made.
    devcontainerStatus.mockResolvedValueOnce(status()).mockRejectedValue(new Error('offline'))
    renderChat()

    const button = await screen.findByRole('button', { name: /^trust$/i })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByText('Dev Container trusted')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /got it/i }))
    await waitFor(() => expect(screen.queryByText('Dev Container trusted')).not.toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /^trust$/i })).not.toBeInTheDocument()
  })

  it('keeps the prompt suppressed across later polls once trust was withdrawn', async () => {
    // Withdrawal is an explicit "no" and is remembered, so unlike an
    // acknowledgement it must survive every subsequent untrusted poll.
    devcontainerStatus
      .mockResolvedValueOnce(status({ trusted: true, running: true, container_id: 'aabbccddeeff' }))
      .mockResolvedValue(status({ trusted: false, running: true, container_id: 'aabbccddeeff' }))
    const { queryClient } = renderChat()

    const chip = await screen.findByRole('button', { name: 'Dev Container' })
    fireEvent.click(chip)
    fireEvent.click(screen.getByRole('menuitem', { name: /withdraw trust/i }))
    await waitFor(() => expect(devcontainerUntrust).toHaveBeenCalledWith(PROJECT))
    await waitFor(() => expect(devcontainerStatus).toHaveBeenCalledTimes(2))

    await pollStatus(queryClient)
    await pollStatus(queryClient)
    expect(screen.queryByText(/run this project's agent inside its dev container\?/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^trust$/i })).not.toBeInTheDocument()
  })

  it('asks again after the config is edited, even though the user said "not now"', async () => {
    // A dismissal is a decision about the bytes the user was shown. Keyed on the
    // config PATH it outlived an edit -- and an edit is what drops trust, so the
    // prompt was suppressed at exactly the moment it had to reappear: the session
    // would go on running on the host while the project's container config had
    // changed under a "no" the user gave to different content.
    devcontainerStatus.mockResolvedValue(status())
    const { queryClient } = renderChat()
    await screen.findByRole('button', { name: /^trust$/i })

    fireEvent.click(screen.getByRole('button', { name: /not now/i }))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /^trust$/i })).not.toBeInTheDocument(),
    )

    // The config is edited: same path, new digest.
    devcontainerStatus.mockResolvedValue(status({ config_digest: 'digest-after-the-edit' }))
    await pollStatus(queryClient)

    await waitFor(() => expect(screen.getByRole('button', { name: /^trust$/i })).toBeInTheDocument())
  })

  it('keeps the dismissal while the config is unchanged', async () => {
    // The control for the test above: if any re-poll revived the prompt, the
    // dismissal would be worthless and the digest binding unproven.
    devcontainerStatus.mockResolvedValue(status())
    const { queryClient } = renderChat()
    await screen.findByRole('button', { name: /^trust$/i })

    fireEvent.click(screen.getByRole('button', { name: /not now/i }))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /^trust$/i })).not.toBeInTheDocument(),
    )

    for (let i = 0; i < 2; i++) {
      await pollStatus(queryClient)
    }
    expect(screen.queryByRole('button', { name: /^trust$/i })).not.toBeInTheDocument()
  })

  it('asks again after an edit even when trust was explicitly withdrawn', async () => {
    // Withdrawal is a "no" to the config in front of the user, not to the path
    // forever. On the path key it also outlived an edit, so a genuinely different
    // config inherited a refusal nobody gave it.
    devcontainerStatus
      .mockResolvedValueOnce(status({ trusted: true, running: true, container_id: 'aabbccddeeff' }))
      .mockResolvedValue(status({ trusted: false, running: true, container_id: 'aabbccddeeff' }))
    const { queryClient } = renderChat()
    const chip = await screen.findByRole('button', { name: 'Dev Container' })
    fireEvent.click(chip)
    fireEvent.click(screen.getByRole('menuitem', { name: /withdraw trust/i }))
    await waitFor(() => expect(devcontainerUntrust).toHaveBeenCalledWith(PROJECT))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /^trust$/i })).not.toBeInTheDocument(),
    )

    devcontainerStatus.mockResolvedValue(
      status({ trusted: false, running: true, container_id: 'aabbccddeeff', config_digest: 'digest-after-the-edit' }),
    )
    await pollStatus(queryClient)

    await waitFor(() => expect(screen.getByRole('button', { name: /^trust$/i })).toBeInTheDocument())
  })

  it('does not share a dismissal between two configs that cannot be hashed', async () => {
    // A refused config reports no digest and can never be trusted. It gets its own
    // stable key rather than silently inheriting the last hashable one's.
    devcontainerStatus.mockResolvedValue(status({ config_digest: null }))
    const { queryClient } = renderChat()
    await screen.findByRole('button', { name: /^trust$/i })
    fireEvent.click(screen.getByRole('button', { name: /not now/i }))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /^trust$/i })).not.toBeInTheDocument(),
    )

    // Now it hashes: a different config, so a different question.
    devcontainerStatus.mockResolvedValue(status({ config_digest: 'now-it-hashes' }))
    await pollStatus(queryClient)
    await waitFor(() => expect(screen.getByRole('button', { name: /^trust$/i })).toBeInTheDocument())
  })
})
