/**
 * Session Back/Forward across a visit to another page.
 *
 * The chat page unmounts when the user leaves /chat, and a Back to it mounts it
 * again on a POP. The session entries pushed before the visit must stay
 * walkable from that fresh mount, not only after its own first navigation.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route, useLocation, useNavigate, type NavigateFunction } from 'react-router-dom'
import { createConnectedTestStore } from './helpers'
import { switchSlot } from '../store/chatSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

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
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'generateTitle', 'kirocrewConfig', 'agentResolvedModel',
     'chatSlotsUnrestored', 'sessionsNamed'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {}
    )])
  ),
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

const slot = (key: string, title: string): ChatSlot => ({
  key, title, messages: 0, running: false, mode: '', created: '', last_ts: '',
})
const A = slot('chat-1-100', 'Alpha')
const B = slot('chat-2-200', 'Beta')
const C = slot('chat-3-300', 'Gamma')

let url = ''
let navigate: NavigateFunction = () => {}
function RouterProbe() {
  const loc = useLocation()
  url = loc.pathname + loc.search
  navigate = useNavigate()
  return null
}

function renderChat() {
  const store = createConnectedTestStore({
    dashboard: { status: { platform: 'darwin' }, slots: [A, B, C], slotsLoaded: true },
  } as Partial<RootState>)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage />} />
              <Route path="/settings" element={<div>settings</div>} />
            </Routes>
            <RouterProbe />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

const sid = () => new URLSearchParams(url.split('?')[1] ?? '').get('sid')

/** One history step, then wait until both the URL and the view name `key`. */
async function stepTo(store: ReturnType<typeof createConnectedTestStore>, delta: -1 | 1, key: string) {
  await act(async () => { navigate(delta) })
  await waitFor(() => {
    expect(store.getState().chat.activeSlot).toBe(key)
    expect(sid()).toBe(key)
  })
}

beforeEach(() => {
  localStorage.clear()
  url = ''
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('session Back/Forward across a visit to another page', () => {
  it('retraces the sessions visited before the visit, then Forward', async () => {
    const store = renderChat()
    await waitFor(() => expect(sid()).toBe(A.key))
    await act(async () => { await store.dispatch(switchSlot(B.key)) })
    await waitFor(() => expect(sid()).toBe(B.key))
    await act(async () => { await store.dispatch(switchSlot(C.key)) })
    await waitFor(() => expect(sid()).toBe(C.key))

    await act(async () => { navigate('/settings') })
    await waitFor(() => expect(url).toBe('/settings'))
    // Back to the chat mounts it again, on a POP, standing on C.
    await act(async () => { navigate(-1) })
    await waitFor(() => expect(url.startsWith('/chat')).toBe(true))
    expect(sid()).toBe(C.key)

    await stepTo(store, -1, B.key)
    await stepTo(store, -1, A.key)
    await stepTo(store, 1, B.key)
  })
})
