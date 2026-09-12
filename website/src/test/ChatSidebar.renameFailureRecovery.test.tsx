/**
 * Regression for #10151: a server-refused sidebar rename left the optimistic
 * title on screen forever.
 *
 * The commit path dispatches `sseSlotTitle` optimistically, then calls
 * `api.renameSlot`. The old `.catch` recovered with
 * `queryClient.invalidateQueries({ queryKey: ['chat-slots'] })` -- a no-op,
 * because no React Query is registered on a plain ['chat-slots'] key (the only
 * matching keys are one-shot `fetchQuery` calls with `gcTime: 0` in
 * useSessionActions). Slot titles live in the Redux dashboard slice, so the
 * recovery must be `dispatch(fetchSlots())`: its `fulfilled` reconciler
 * overwrites the stale optimistic title with the server truth.
 *
 * The test drives the real inline-rename UI (double-click -> textarea -> blur
 * commits), rejects `api.renameSlot`, and asserts the store title snaps back
 * to the server value delivered by `api.chatSlots`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const SERVER_TITLE = 'Server Title'
const SLOT_KEY = 'chat-rename-recovery-1'

const { renameSlotMock, chatSlotsMock } = vi.hoisted(() => ({
  renameSlotMock: vi.fn(),
  chatSlotsMock: vi.fn(),
}))

// Every other api method resolves empty; the two named mocks drive the test.
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'renameSlot') return renameSlotMock
      if (prop === 'chatSlots') return chatSlotsMock
      return vi.fn().mockResolvedValue([])
    },
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

import ChatSidebar from '../pages/ChatSidebar'
import type { ChatSlot } from '../types'

const slot = { key: SLOT_KEY, title: SERVER_TITLE, running: false, tags: [], created: '', last_ts: '' } as unknown as ChatSlot

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: [slot], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], [])
  qc.setQueryData(['chat-folders'], [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[slot]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, container: view.container }
}

// The row (and its textarea) can be remounted on every re-render under the
// framer-motion mock (plain elements swap instead of morphing), so events must
// fire on a freshly-queried node, never a stale reference.
function currentTextarea(container: HTMLElement): HTMLTextAreaElement {
  const wrap = container.querySelector(`[data-slot-key="${SLOT_KEY}"]`) as HTMLElement
  const textarea = wrap?.querySelector('textarea') as HTMLTextAreaElement
  expect(textarea).toBeTruthy()
  return textarea
}

function commitRename(container: HTMLElement, draft: string) {
  const wrap = container.querySelector(`[data-slot-key="${SLOT_KEY}"]`) as HTMLElement
  expect(wrap).toBeTruthy()
  const row = wrap.querySelector('.session-row') as HTMLElement
  expect(row).toBeTruthy()
  const title = within(row).getByTitle(SERVER_TITLE)
  fireEvent.click(title, { detail: 1 })
  fireEvent.click(title, { detail: 2 })
  fireEvent.doubleClick(title, { detail: 2 })
  fireEvent.change(currentTextarea(container), { target: { value: draft } })
  fireEvent.blur(currentTextarea(container))
}

const titleInStore = (store: ReturnType<typeof createTestStore>) =>
  store.getState().dashboard.slots.find(s => s.key === SLOT_KEY)?.title

beforeEach(() => {
  localStorage.clear()
  renameSlotMock.mockReset()
  chatSlotsMock.mockReset()
  chatSlotsMock.mockResolvedValue([slot])
})
afterEach(() => vi.clearAllMocks())

describe('sidebar rename failure recovery (#10151)', () => {
  it('snaps the title back to the server value when the rename is refused', async () => {
    renameSlotMock.mockRejectedValue(new Error('rename refused'))
    const { store, container } = renderSidebar()

    commitRename(container, 'Optimistic Draft')

    // Optimistic write lands first (this is the value that used to stick forever).
    expect(titleInStore(store)).toBe('Optimistic Draft')
    expect(renameSlotMock).toHaveBeenCalledWith(SLOT_KEY, 'Optimistic Draft')

    // Recovery: the .catch dispatches fetchSlots(), whose fulfilled reconciler
    // restores the authoritative server title.
    await waitFor(() => expect(chatSlotsMock).toHaveBeenCalled())
    await waitFor(() => expect(titleInStore(store)).toBe(SERVER_TITLE))

    // The revert is not silent: the failure renders through ErrorNotice
    // (errors-use-error-notice), so the user sees why the title snapped back.
    await waitFor(() => expect(container.querySelector('[data-testid="rename-error"]')).toBeTruthy())
  })

  it('keeps the optimistic title and never refetches when the rename succeeds', async () => {
    renameSlotMock.mockResolvedValue({})
    const { store, container } = renderSidebar()

    commitRename(container, 'Accepted Title')

    expect(titleInStore(store)).toBe('Accepted Title')
    await waitFor(() => expect(renameSlotMock).toHaveBeenCalledWith(SLOT_KEY, 'Accepted Title'))
    // Let the resolved promise settle: no recovery refetch may fire on success.
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(chatSlotsMock).not.toHaveBeenCalled()
    expect(titleInStore(store)).toBe('Accepted Title')
  })
})
