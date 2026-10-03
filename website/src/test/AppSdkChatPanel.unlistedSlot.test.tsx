/**
 * The app SDK ChatPanel must show the slot it is handed even when the
 * dashboard's slot list does not list that slot yet.
 *
 * An app creates a session with `POST /api/chat/slots` and mounts the panel
 * straight away; the slot list only learns of the new session from the next
 * live push. Before that push, ChatPage cleared the unknown active slot and
 * restored the last-used session from `mc-active-slot-chat`, so the panel
 * showed an unrelated conversation and the composer sent into it.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { render, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import dashboardReducer from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

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
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => ({ contentWidth: 'compact' }), CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } } }))

vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

// `chatSlots` never settles: the test runs inside the window before the
// dashboard hears about the new slot, which is where the bug lives.
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planAction', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'generateTitle'].map(k => [k, k === 'chatSlots'
      ? vi.fn(() => new Promise(() => {}))
      : vi.fn().mockResolvedValue(k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {})])
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

import ChatPanel from '../app-sdk/ChatPanel'

const LAST_USED = 'chat-1-100'
const NEW_SLOT = 'app-new-1'

const slot = (key: string): ChatSlot => ({
  key, title: key, messages: 0, running: false, mode: '', created: '', last_ts: '',
})

function renderPanel(dashboard: Partial<RootState['dashboard']> = {}) {
  const store = createTestStore({
    dashboard: {
      ...dashboardReducer(undefined, { type: '@@INIT' }),
      slots: [slot(LAST_USED)],
      ...dashboard,
    },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/apps/diagram']}>
            <ChatPanel slotKey={NEW_SLOT} conversationOnly />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

// Let every mount effect, thunk and follow-up render settle.
const settle = () => act(async () => { await new Promise(r => setTimeout(r, 50)) })

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-active-slot-chat', LAST_USED)
})

afterEach(() => vi.clearAllMocks())

describe('app SDK ChatPanel with a slot the slot list does not have yet', () => {
  it('keeps the handed slot active instead of restoring the last-used one', async () => {
    const store = renderPanel()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    await settle()
    expect(store.getState().chat.activeSlot).toBe(NEW_SLOT)
    expect(store.getState().dashboard.slots.map(s => s.key)).toContain(NEW_SLOT)
  })

  it('does not re-add a slot the user is closing', async () => {
    const store = renderPanel({
      closingSlots: {
        [NEW_SLOT]: { requestId: 'r1', inFlightUntil: Date.now() + 60_000, graceFrames: 0, awaitingFetches: [], confirmedUntil: null },
      } as RootState['dashboard']['closingSlots'],
    })
    await settle()
    expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain(NEW_SLOT)
    expect(store.getState().dashboard.closingSlots[NEW_SLOT]).toBeDefined()
  })
})
