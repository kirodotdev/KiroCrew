/**
 * A ChatPage that renders a transcript tells the unread gate the active slot
 * is on screen, on any route: an app's ChatPanel shows its session there.
 * The sessions-only embed renders no transcript, so it does not.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
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
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null, PinnedPrompt: () => null }))
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
import ChatPage from '../pages/ChatPage'
import { isChatTranscriptMounted } from '../lib/mountedChatTranscript'

const SLOT = 'app-slot-1'

const slot = (key: string): ChatSlot => ({
  key, title: key, messages: 0, running: false, mode: '', created: '', last_ts: '',
})

function renderAt(node: ReactNode) {
  const store = createTestStore({
    dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), slots: [slot(SLOT)] } as RootState['dashboard'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/apps/notes']}>{node}</MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => { localStorage.clear() })
afterEach(() => vi.clearAllMocks())

describe('ChatPage transcript count', () => {
  it('counts an app ChatPanel while it shows its session, and stops on unmount', async () => {
    const view = renderAt(<ChatPanel slotKey={SLOT} conversationOnly />)
    await waitFor(() => expect(isChatTranscriptMounted()).toBe(true))
    view.unmount()
    expect(isChatTranscriptMounted()).toBe(false)
  })

  it('does not count an app ChatPanel its container hides', async () => {
    const proto = Element.prototype as unknown as { checkVisibility?: () => boolean }
    proto.checkVisibility = function (this: Element) { return !this.closest('[data-hidden-host]') }
    try {
      const view = renderAt(<div data-hidden-host=""><ChatPanel slotKey={SLOT} conversationOnly /></div>)
      await waitFor(() => expect(view.container.querySelector('[data-chat-scope]')).not.toBeNull())
      expect(isChatTranscriptMounted()).toBe(false)
      view.container.querySelector('[data-hidden-host]')?.removeAttribute('data-hidden-host')
      expect(isChatTranscriptMounted()).toBe(true)
      view.unmount()
    } finally { delete proto.checkVisibility }
  })

  it('counts the chat embed but not the sessions-only embed', () => {
    const chat = renderAt(<ChatPage embedded embedMode="chat" />)
    expect(isChatTranscriptMounted()).toBe(true)
    chat.unmount()
    const sessions = renderAt(<ChatPage embedded embedMode="sessions" />)
    expect(isChatTranscriptMounted()).toBe(false)
    sessions.unmount()
  })
})
