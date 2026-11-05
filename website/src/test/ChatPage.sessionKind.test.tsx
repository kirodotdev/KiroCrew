/**
 * An app-drawn session (`contributes.sessionKinds`) in the chat pane.
 *
 * A slot carrying `session_kind` mounts the declaring app's `entry` through
 * `AppHost`, with its binding, in place of the transcript and composer. When the
 * app cannot draw it (disabled, uninstalled, or no longer declaring the kind) the
 * transcript shows read-only with a notice and no composer.
 *
 * Same ChatPage render scaffold as ChatPage.popoutClosedNotice.test.tsx; `AppHost`
 * is stood in by a recorder so the assertion is on what the page hands it.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

type PreloadState = { dashboard?: Partial<RootState['dashboard']>; chat?: Partial<RootState['chat']> }

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: () => null }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null, UserMessage: () => null, CronAckBar: () => null, NotificationItem: () => null, PinnedPrompt: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => ({ contentWidth: 'compact' }), CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } } }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

/** Every AppHost render, newest last. */
const hosted = vi.hoisted(() => ({ props: [] as Record<string, unknown>[] }))
vi.mock('../components/AppHost', () => ({
  default: (props: Record<string, unknown>) => {
    hosted.props.push(props)
    return <div data-testid="app-host-mock" />
  },
}))

const apps = vi.hoisted(() => ({ list: [] as unknown[] }))
vi.mock('../api/client', () => ({
  api: {
    ...Object.fromEntries(
      ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
       'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
       'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
       'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
       'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
       'notifications', 'status', 'generateTitle', 'kirocrewConfig', 'agentResolvedModel',
       'chatSlotsUnrestored', 'sessionsNamed'].map(k => [k, vi.fn().mockResolvedValue(
        k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
      )]),
    ),
    listApps: vi.fn(() => Promise.resolve(apps.list)),
  },
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatPage from '../pages/ChatPage'
import { i18nT } from '../i18n/t'
import { api } from '../api/client'
import { sseChatMessage } from '../store/chatSlice'

const OBJECT = '0123456789abcdef0123456789abcdef'
const slot = (key: string, over: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title: key, messages: 0, running: false, mode: '', created: '', last_ts: '', ...over,
})
const bound = slot('chat-1-100', { title: 'My project', session_kind: { app: 'project-apps', kind: 'project', object: OBJECT } })
const plain = slot('chat-2-200', { title: 'Plain chat' })

const projectKind = { id: 'project', title: 'Project app', menuLabel: 'New project app', icon: 'AppWindow', entry: 'dist/pane.mjs', defaultAgent: 'kirocrew-conductor' }
const projectApps = (over: Record<string, unknown> = {}) => ({
  name: 'project-apps', displayName: 'Project Apps', version: '0.1.0', enabled: true,
  manifest: { name: 'project-apps', contributes: { sessionKinds: [projectKind] } }, ...over,
})

function renderChatPage(activeSlot: string, slots: ChatSlot[]) {
  const preload: PreloadState = {
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots, slotsLoaded: true, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    },
    chat: {
      activeSlot, messages: [], slotCursorKey: null, slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'logs', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false, slotRun: {},
    },
  }
  const store = createTestStore(preload as Partial<RootState>)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[`/chat?sid=${activeSlot}`]}>
            <Routes><Route path="/chat/:slug?" element={<ChatPage />} /></Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store }
}

const composer = () => screen.queryByTestId('composer-dock-root')

beforeEach(() => {
  localStorage.clear()
  hosted.props = []
  apps.list = [projectApps()]
})
afterEach(() => vi.clearAllMocks())

describe('ChatPage: an app-drawn session', () => {
  it('mounts the kind’s entry through AppHost with the session binding, in place of the composer', async () => {
    renderChatPage(bound.key, [bound, plain])
    await screen.findByTestId('app-host-mock')
    const props = hosted.props[hosted.props.length - 1]
    expect((props.app as { name: string }).name).toBe('project-apps')
    expect(props.entry).toBe('dist/pane.mjs')
    expect(props.active).toBe(true)
    expect(props.sessionKey).toBe('dashboard:chat-1-100')
    expect(props.sessionPane).toEqual({
      slotKey: 'chat-1-100', sessionKey: 'dashboard:chat-1-100', app: 'project-apps', kind: 'project', object: OBJECT,
    })
    expect(screen.getByTestId('app-session-pane').getAttribute('data-session-kind')).toBe('project-apps:project')
    expect(composer()).toBeNull()
    expect(screen.queryByTestId('app-session-fallback')).toBeNull()
  })

  it('keeps the ordinary transcript and composer for a slot with no binding', async () => {
    renderChatPage(plain.key, [bound, plain])
    await waitFor(() => expect(composer()).toBeTruthy())
    expect(screen.queryByTestId('app-session-pane')).toBeNull()
    expect(screen.queryByTestId('app-host-mock')).toBeNull()
  })

  it('falls back to the read-only transcript with a notice and no composer when the app is disabled', async () => {
    apps.list = [projectApps({ enabled: false })]
    renderChatPage(bound.key, [bound, plain])
    const notice = await screen.findByTestId('app-session-unavailable')
    expect(notice.getAttribute('role')).toBe('status')
    expect(notice.textContent).toContain(i18nT('pages.chat.appSessionPane.unavailable_read_only', { app: 'Project Apps' }))
    expect(composer()).toBeNull()
    expect(screen.queryByTestId('app-host-mock')).toBeNull()
  })

  it('falls back the same way when the app is not installed, naming the bound app', async () => {
    apps.list = []
    renderChatPage(bound.key, [bound, plain])
    const notice = await screen.findByTestId('app-session-unavailable')
    expect(notice.textContent).toContain(i18nT('pages.chat.appSessionPane.unavailable_read_only', { app: 'project-apps' }))
    expect(composer()).toBeNull()
  })

  it('reports a failed app list as an error, read-only, rather than as an app that is gone', async () => {
    vi.mocked(api.listApps).mockRejectedValueOnce(new Error('gateway said no'))
    renderChatPage(bound.key, [bound, plain])
    const err = await screen.findByTestId('app-session-apps-load-error')
    expect(err.textContent).toContain(i18nT('pages.chat.appSessionPane.apps_load_failed'))
    expect(err.textContent).toContain('gateway said no')
    expect(screen.queryByTestId('app-session-unavailable')).toBeNull()
    expect(composer()).toBeNull()
  })

  it('draws no approval strip while nothing is waiting', async () => {
    renderChatPage(bound.key, [bound, plain])
    await screen.findByTestId('app-host-mock')
    expect(screen.queryByTestId('app-session-approvals')).toBeNull()
  })

  it('draws the slot’s pending tool approval as native controls over the pane, deciding on the slot route', async () => {
    const { store } = renderChatPage(bound.key, [{ ...bound, running: true, pending_approval: true }, plain])
    await screen.findByTestId('app-host-mock')
    act(() => {
      store.dispatch(sseChatMessage({
        slot: bound.key, role: 'permission', content: 'Running: npm run build',
        meta: { approval_id: 'ap-9', mid: 'mid-9', tool_input: '{"command":"npm run build"}', is_shell: '1', full_command: 'npm run build', base_command: 'npm', tool_call_id: 'tc-9' },
      }))
    })
    const strip = await screen.findByTestId('app-session-approvals')
    // Over the app, in the pane's bottom-right band.
    expect(screen.getByTestId('app-session-pane').contains(strip)).toBe(true)
    expect(strip.style.right).toBe('16px')
    expect(strip.style.bottom).toBe('16px')
    expect(composer()).toBeNull()
    const allow = within(strip).getByRole('button', { name: i18nT('components.chatInput.allow_once') })
    expect(within(strip).getByRole('button', { name: 'Reject' })).toBeTruthy()
    fireEvent.click(allow)
    await waitFor(() => expect(api.approveChatSlot).toHaveBeenCalledWith(bound.key, 'approved', {
      request_id: 'ap-9', response_id: expect.stringMatching(/^r_/), origin: 'native', request_mid: 'mid-9',
    }))
    // The decision settles the card, and the strip goes with it.
    await waitFor(() => expect(screen.queryByTestId('app-session-tool-approval')).toBeNull())
  })

  it('falls back when the installed app no longer declares the kind', async () => {
    apps.list = [projectApps({ manifest: { name: 'project-apps', contributes: { sessionKinds: [{ ...projectKind, id: 'other' }] } } })]
    renderChatPage(bound.key, [bound, plain])
    await screen.findByTestId('app-session-unavailable')
    expect(composer()).toBeNull()
    expect(screen.queryByTestId('app-host-mock')).toBeNull()
  })
})
