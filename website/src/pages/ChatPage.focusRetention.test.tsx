/**
 * The composer keeps its DOM node, focus and caret while the page around it
 * re-renders. ChatPage renders its layout through module-level view components
 * (pages/chat/page/*.tsx); a view whose identity changed between renders would
 * remount everything under it, and the first sign would be a composer that
 * loses focus mid-sentence. Pinned across the three things that re-render the
 * page most: a plain parent re-render, a slot switch and the sessions-sidebar
 * pin toggle. The same harness pins one ordering the page owns: the composer
 * dock renders after the transcript scroller.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, act, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from '../test/helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs } from '../hooks/usePanelTabs'
import { switchSlot, appendMessage } from '../store/chatSlice'
import { i18nT } from '../i18n/t'

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
// The composer stand-in: an uncontrolled textarea, so its node, focus and caret
// survive exactly as long as React keeps the element mounted.
vi.mock('../components/ChatInput', () => ({ default: () => <textarea data-testid="composer-textarea" aria-label="Message" /> }))
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
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/SidePanel', () => ({
  default: () => <div data-testid="side-panel" />,
  SIDE_PANEL_MIN_W: 320,
  SIDE_PANEL_RESERVED_W: 560,
  CHAT_PANE_MIN_W: 320,
  sidePanelFillWidth: () => undefined,
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, k) => vi.fn().mockResolvedValue(k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {}),
  }),
}))
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import ChatPage from './ChatPage'

function tree(store: ReturnType<typeof createTestStore>, queryClient: QueryClient) {
  return (
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <Routes><Route path="/chat/:slug?" element={<ChatPage />} /></Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
}

/** A page on slot-a with a transcript, so the composer sits in the dock, focused mid-word. Every step runs
 *  inside an awaited act(), so the page's mount queries (the mocked api, the theme boot) settle inside it. */
async function mountFocused() {
  const store = createTestStore()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  let view!: ReturnType<typeof render>
  await act(async () => { view = render(tree(store, queryClient)) })
  // Two listed sessions: the sidebar pin toggle refuses to collapse an empty list.
  const slot = (key: string) => ({ key, title: key, messages: 2, running: false, mode: '', created: '', last_ts: '' })
  await act(async () => { store.dispatch({ type: 'dashboard/sseSlots', payload: [slot('slot-a'), slot('slot-b')] }) })
  await act(async () => { store.dispatch(switchSlot.pending('r1', 'slot-a')) })
  await act(async () => {
    store.dispatch(appendMessage({ role: 'user', content: 'hi', ts: '1' } as never))
    store.dispatch(appendMessage({ role: 'assistant', content: 'hello', ts: '2' } as never))
  })
  const box = screen.getByTestId('composer-textarea') as HTMLTextAreaElement
  await act(async () => {
    box.focus()
    box.value = 'draft text'
    box.setSelectionRange(5, 5)
  })
  return { store, queryClient, view, box }
}

function expectStillFocused(box: HTMLTextAreaElement) {
  const now = screen.getByTestId('composer-textarea') as HTMLTextAreaElement
  expect(now).toBe(box)
  expect(document.activeElement).toBe(box)
  expect(box.value).toBe('draft text')
  expect(box.selectionStart).toBe(5)
  expect(box.selectionEnd).toBe(5)
}

describe('ChatPage — the composer keeps focus and caret', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
    localStorage.clear()
    __resetPanelTabs()
  })

  it('across a parent re-render', async () => {
    const { store, queryClient, view, box } = await mountFocused()
    await act(async () => { view.rerender(tree(store, queryClient)) })
    await act(async () => { store.dispatch(appendMessage({ role: 'assistant', content: 'more', ts: '3' } as never)) })
    expectStillFocused(box)
  })

  it('across a slot switch and back', async () => {
    const { store, box } = await mountFocused()
    await act(async () => { store.dispatch(switchSlot.pending('r2', 'slot-b')) })
    await act(async () => { store.dispatch(switchSlot.pending('r3', 'slot-a')) })
    expect(screen.getByTestId('composer-textarea')).toBe(box)
  })

  it('across the sessions-sidebar pin toggle', async () => {
    const { box } = await mountFocused()
    await act(async () => { window.dispatchEvent(new Event('toggle-pin-chat-sidebar')) })
    await act(async () => { window.dispatchEvent(new Event('toggle-pin-chat-sidebar')) })
    expectStillFocused(box)
  })
})

describe('ChatPage — the composer dock follows the transcript', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
    localStorage.clear()
    __resetPanelTabs()
  })

  it('renders the floating dock root after the scroller', async () => {
    // The dock is the next positioned sibling after the scroller: that DOM order, not a z-index, is
    // what paints it over the transcript. ChatPage places the two views, so the order is the page's.
    await mountFocused()
    const scroller = screen.getByLabelText(i18nT('pages.chatPage.chat_messages'))
    const dock = screen.getByTestId('composer-dock-root')
    expect(scroller.compareDocumentPosition(dock) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
