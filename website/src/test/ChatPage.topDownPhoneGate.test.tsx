/**
 * The full chat page applies the opt-in top-down layout (Settings > Chat >
 * `topDownLayout`) at desktop width only. On a phone the thumb reach and the
 * on-screen keyboard sit at the bottom, so the page keeps the bottom composer
 * even with the setting on. ChatPane.topDownDock.test.tsx pins the same gate
 * for a split pane; this file mounts the page itself.
 *
 * The marker is the composer dock root (`data-testid="composer-dock-root"`),
 * the box the page's `topDown` decision positions. In the top-down layout it
 * is anchored to the top (no `bottom-0`, an inline `top`) and carries the
 * top-overlay attribute that tells the transcript to clear it at the top. In
 * the bottom layout it is pinned with `bottom-0`, has no inline `top`, and
 * carries no top-overlay attribute.
 *
 * The same harness pins the top-down dock's flex structure at its height cap:
 * the status stack yields first, the cards above the composer scroll, and the
 * composer row keeps its height. ChatPane.topDownDock.test.tsx pins the same
 * structure for a split pane.
 *
 * The harness is the one ChatPage.mobileSingleTopbar.test.tsx uses: the heavy
 * children are stubbed, `useIsMobile` reads a hoisted flag, and the real
 * `loadChatConfig` reads the setting from localStorage.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, cleanup, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sseSlots, sseConnected } from '../store/dashboardSlice'
import { setActiveSlot, setQuestionCard } from '../store/chatSlice'
import { TOP_OVERLAY_ATTR, TOP_OVERLAY_GROWS } from '../pages/chat/browserLiveClearance'

vi.mock('framer-motion', async (importOriginal) => {
  const actual = await importOriginal<typeof import('framer-motion')>()
  return {
    ...actual,
    animate: () => ({ stop: () => {} }),
  }
})
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => <textarea aria-label="Chat message" /> }))
vi.mock('../pages/chat/page/ChatTranscriptView', () => ({ default: () => <button type="button" data-testid="transcript-focusable">Transcript action</button> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null }))
vi.mock('../pages/ChatSidebar', () => ({
  default: () => <div data-testid="sidebar-stub" />,
  SIDEBAR_MIN: 200,
  SIDEBAR_MAX: 500,
}))
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
const viewport = vi.hoisted(() => ({ isMobile: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => viewport.isMobile }))
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
      'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
      'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
      'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
      'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
      'notifications', 'status', 'generateTitle', 'dashboardConfig', 'projectGit',
      'mcpActive', 'mcpServers', 'kirocrewConfig'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
    )]),
  ),
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
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
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as never
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import ChatPage from '../pages/ChatPage'

// The key loadChatConfig reads (pages/chat/ChatSettings.tsx).
const CHAT_CONFIG_KEY = 'mc-chat-config'

async function renderChatDock(setup?: (store: ReturnType<typeof createTestStore>) => void) {
  const store = createTestStore()
  act(() => {
    store.dispatch(sseConnected())
    store.dispatch(sseSlots([{ key: 'slot-0', title: 'Top-down gate' }] as never))
    store.dispatch(setActiveSlot('slot-0'))
    setup?.(store)
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const { findByTestId } = render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat?sid=slot-0']}>
            <Routes><Route path="/chat/:slug?" element={<ChatPage />} /></Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return findByTestId('composer-dock-root')
}

describe('ChatPage with the top-down layout turned on', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: true }))
  })
  afterEach(() => {
    cleanup()
    localStorage.clear()
    viewport.isMobile = false
  })

  it('puts the composer before transcript controls only in top-down mode without remounting it', async () => {
    const dock = await renderChatDock()
    const composer = within(dock).getByRole('textbox')
    const transcriptControl = screen.getByTestId('transcript-focusable')

    expect(composer.compareDocumentPosition(transcriptControl) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    composer.focus()
    localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: false }))
    await act(async () => { window.dispatchEvent(new Event('mc-config-changed')) })
    await waitFor(() => expect(screen.getByTestId('composer-dock-root').className.split(/\s+/)).toContain('bottom-0'))

    const bottomDock = screen.getByTestId('composer-dock-root')
    const bottomComposer = within(bottomDock).getByRole('textbox')
    expect(bottomComposer).toBe(composer)
    expect(document.activeElement).toBe(composer)
    expect(composer.compareDocumentPosition(transcriptControl) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
  })

  it('puts the composer at the top of the page on a desktop screen', async () => {
    viewport.isMobile = false
    const dock = await renderChatDock()
    expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'pointer-events-none']))
    expect(dock.className.split(/\s+/)).not.toContain('bottom-0')
    expect(dock.style.top).not.toBe('')
    expect(dock.getAttribute(TOP_OVERLAY_ATTR)).toBe(TOP_OVERLAY_GROWS)
  })

  it('keeps the composer at the bottom of the page on a phone', async () => {
    viewport.isMobile = true
    const dock = await renderChatDock()
    expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'bottom-0', 'pointer-events-none']))
    expect(dock.style.top).toBe('')
    expect(dock.hasAttribute(TOP_OVERLAY_ATTR)).toBe(false)
  })

  // At the dock's cap (`topDownDockMaxHeight`) the status stack yields first,
  // then the cards above the composer scroll, and the composer row holds.
  // happy-dom has no layout, so these pin the flex structure the CSS relies on.
  const withQuestion = (store: ReturnType<typeof createTestStore>) => {
    store.dispatch(setQuestionCard({ slot: 'slot-0', ask_id: 'ask-1', questions: [{ question: 'Which?', options: [{ label: 'A' }] }] } as never))
  }
  const classes = (el: Element) => el.className.split(/\s+/)

  it('scrolls the cards above the composer at the cap and never shrinks the composer row', async () => {
    const dock = await renderChatDock(withQuestion)
    const stack = within(dock).getByTestId('composer-status-stack')
    const precards = within(dock).getByTestId('composer-precards')
    const inputArea = precards.parentElement as HTMLElement
    const card = await within(dock).findByTestId('composer-question-card')
    const composer = within(dock).getByRole('textbox', { name: 'Chat message' })
    const row = [...inputArea.children].find(child => child.contains(composer)) as HTMLElement
    expect(classes(stack)).toContain('shrink-[1000]')
    expect(classes(inputArea)).toEqual(expect.arrayContaining(['relative', 'z-10', 'dock-inert', 'flex', 'flex-col', 'min-h-0']))
    expect(classes(precards)).toEqual(expect.arrayContaining(['min-h-0', 'overflow-y-auto', 'overscroll-contain', 'dock-inert']))
    expect(precards.contains(card)).toBe(true)
    expect(precards.contains(composer)).toBe(false)
    expect(row).not.toBe(precards)
    expect(classes(row)).toContain('shrink-0')
  })

  it('keeps the bottom input area boxes: the cards wrapper makes no box', async () => {
    localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: false }))
    const dock = await renderChatDock(withQuestion)
    const stack = within(dock).getByTestId('composer-status-stack')
    const precards = within(dock).getByTestId('composer-precards')
    const inputArea = precards.parentElement as HTMLElement
    const card = await within(dock).findByTestId('composer-question-card')
    const composer = within(dock).getByRole('textbox', { name: 'Chat message' })
    const row = [...inputArea.children].find(child => child.contains(composer)) as HTMLElement
    expect(classes(dock)).toContain('bottom-0')
    expect(inputArea.className).toBe('relative z-10 dock-inert')
    expect(precards.className).toBe('contents')
    expect(precards.contains(card)).toBe(true)
    expect(row.className).toBe('')
    expect(classes(stack)).not.toContain('shrink-[1000]')
  })
})
