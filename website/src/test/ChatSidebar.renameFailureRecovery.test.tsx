/**
 * Regression for #10151: a server-refused sidebar rename left the optimistic
 * title on screen forever.
 *
 * The commit path dispatches `sseSlotTitle` optimistically, then calls
 * `api.renameSlot`. The old `.catch` recovered with
 * `queryClient.invalidateQueries({ queryKey: ['chat-slots'] })` -- a no-op,
 * because no React Query is registered on a plain ['chat-slots'] key (the only
 * matching keys are one-shot `fetchQuery` calls with `gcTime: 0` in
 * useSessionActions). Slot titles live in the Redux dashboard slice.
 *
 * Recovery reconciles ONLY the refused slot, not the whole list: it fetches the
 * server slot list, takes this slot's server title, and writes it back via
 * `sseSlotTitle` under a compare-and-set (revert only while the store title is
 * still the refused optimistic value). A whole-list `dispatch(fetchSlots())`
 * would run `applySlots` and could overwrite a fresher `sseSlotTitle` frame for
 * ANY OTHER slot that arrived during the recovery read -- the WS-clobber test
 * below pins exactly that, and reddens if recovery reverts to a full-list apply.
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
import { sseSlotTitle } from '../store/dashboardSlice'

const slot = { key: SLOT_KEY, title: SERVER_TITLE, running: false, tags: [], created: '', last_ts: '' } as unknown as ChatSlot

function renderSidebar(extraSlots: ChatSlot[] = []) {
  const allSlots = [slot, ...extraSlots]
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: allSlots, approvalMode: 'normal',
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
              slots={allSlots} activeSlot={null} unreadSlots={[]}
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

    // Recovery: the .catch fetches the server slot list and writes back only
    // this slot's server title via sseSlotTitle, restoring the authoritative value.
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

  // WS-clobber protection: recovery reconciles ONLY the refused slot. A newer
  // sseSlotTitle frame for a DIFFERENT slot that arrives while the recovery read
  // is in flight must survive. A whole-list dispatch(fetchSlots()) recovery
  // reddens this test, because applySlots would replace the other slot with the
  // (older) server payload, discarding the WS title. This is the crash-data-loss
  // anchor GPT's blocking finding named.
  it('does not clobber another slot\'s newer WS title during recovery', async () => {
    const OTHER_KEY = 'chat-rename-recovery-2'
    const otherSlot = { key: OTHER_KEY, title: 'Other Server Title', running: false, tags: [], created: '', last_ts: '' } as unknown as ChatSlot
    const WS_TITLE = 'Other Live WS Title'

    renameSlotMock.mockRejectedValue(new Error('rename refused'))
    // The server list is stale for the other slot: it carries the OLD title,
    // while the live WS frame (dispatched below) carries the new one.
    let resolveChatSlots: (v: unknown) => void = () => {}
    chatSlotsMock.mockImplementation(() => new Promise(res => { resolveChatSlots = res }))

    const { store, container } = renderSidebar([otherSlot])

    commitRename(container, 'Optimistic Draft')
    expect(titleInStore(store)).toBe('Optimistic Draft')

    // The recovery read has been issued (renameSlot rejected -> chatSlots called).
    await waitFor(() => expect(chatSlotsMock).toHaveBeenCalled())

    // A live WS update for the OTHER slot lands while the recovery read is in flight.
    store.dispatch(sseSlotTitle({ key: OTHER_KEY, title: WS_TITLE }))

    // Now the stale server list resolves (other slot still on its OLD title).
    resolveChatSlots([
      { key: SLOT_KEY, title: SERVER_TITLE },
      { key: OTHER_KEY, title: 'Other Server Title' },
    ])

    // The renamed slot snaps back to its server truth...
    await waitFor(() => expect(titleInStore(store)).toBe(SERVER_TITLE))
    // ...and the other slot KEEPS its newer WS title, not the stale server one.
    const otherTitle = () => store.getState().dashboard.slots.find(s => s.key === OTHER_KEY)?.title
    expect(otherTitle()).toBe(WS_TITLE)
  })

  // Compare-and-set: if a newer authoritative frame changes the RENAMED slot's
  // title while the recovery read is in flight, recovery must yield to it rather
  // than stomp it back to the (now-stale) server value.
  it('yields to a newer frame on the renamed slot instead of reverting', async () => {
    const NEWER_TITLE = 'Newer Authoritative Title'
    renameSlotMock.mockRejectedValue(new Error('rename refused'))
    let resolveChatSlots: (v: unknown) => void = () => {}
    chatSlotsMock.mockImplementation(() => new Promise(res => { resolveChatSlots = res }))

    const { store, container } = renderSidebar()

    commitRename(container, 'Optimistic Draft')
    expect(titleInStore(store)).toBe('Optimistic Draft')
    await waitFor(() => expect(chatSlotsMock).toHaveBeenCalled())

    // A newer frame changes THIS slot's title mid-recovery: the store title is
    // no longer the refused optimistic value, so the compare-and-set must skip.
    store.dispatch(sseSlotTitle({ key: SLOT_KEY, title: NEWER_TITLE }))
    resolveChatSlots([{ key: SLOT_KEY, title: SERVER_TITLE }])

    // Give the recovery continuation a tick to run; it must NOT overwrite NEWER_TITLE.
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(titleInStore(store)).toBe(NEWER_TITLE)
  })

  // Generation guard (GPT F1): the refuse-X -> rename-back-to-X-succeeds race.
  // A first rename to X is refused; its recovery read is still in flight when a
  // SECOND rename to the same string X is committed and SUCCEEDS. The store
  // title equals the refused value X in both attempts, so the title-only
  // compare-and-set would FALSELY match and restore the first attempt's stale
  // server title over the newer accepted one. Only the per-slot generation
  // counter (rec.gen !== myGen once the second attempt bumps it) catches this.
  // Removing the generation guard reddens this test.
  it('does not let a stale earlier recovery stomp a later identical rename', async () => {
    const X = 'Same Title X'
    // First rename rejects; second (identical) resolves.
    renameSlotMock
      .mockRejectedValueOnce(new Error('rename refused'))
      .mockResolvedValueOnce({})
    // Hold the first recovery read open so the second rename lands first.
    let resolveChatSlots: (v: unknown) => void = () => {}
    chatSlotsMock.mockImplementation(() => new Promise(res => { resolveChatSlots = res }))

    const { store, container } = renderSidebar()

    // Attempt 1: commit X, server refuses, recovery read issued (and parked).
    commitRename(container, X)
    expect(titleInStore(store)).toBe(X)
    await waitFor(() => expect(renameSlotMock).toHaveBeenNthCalledWith(1, SLOT_KEY, X))
    await waitFor(() => expect(chatSlotsMock).toHaveBeenCalled())

    // Attempt 2: commit the SAME string X again; this one succeeds. It bumps
    // the slot's rename generation, so attempt 1's parked recovery is now stale.
    commitRename(container, X)
    await waitFor(() => expect(renameSlotMock).toHaveBeenNthCalledWith(2, SLOT_KEY, X))

    // Now the first attempt's stale recovery read resolves with an OLD server
    // title. The generation guard must reject it: the store keeps X.
    resolveChatSlots([{ key: SLOT_KEY, title: SERVER_TITLE }])
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(titleInStore(store)).toBe(X)
  })

  // Both-reject / transport-down (bolichen97 review item): when renameSlot AND
  // the recovery chatSlots read BOTH reject -- e.g. the gateway is unreachable
  // -- there is no authoritative server truth to revert to. The optimistic
  // title deliberately STAYS (guessing a revert value while offline would
  // assert an authority we do not have; the failure is already visible via
  // ErrorNotice, and the next live frame reconciles it). This pins that
  // deliberate behaviour rather than a rollback.
  it('keeps the optimistic title and shows the notice when the recovery read also fails', async () => {
    renameSlotMock.mockRejectedValue(new Error('rename refused'))
    chatSlotsMock.mockRejectedValue(new Error('gateway unreachable'))
    const { store, container } = renderSidebar()

    commitRename(container, 'Optimistic Draft')
    expect(titleInStore(store)).toBe('Optimistic Draft')

    // Recovery read is attempted and also fails.
    await waitFor(() => expect(chatSlotsMock).toHaveBeenCalled())
    // The failure is surfaced through ErrorNotice...
    await waitFor(() => expect(container.querySelector('[data-testid="rename-error"]')).toBeTruthy())
    // ...and the optimistic title is deliberately left in place (no guess).
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(titleInStore(store)).toBe('Optimistic Draft')
  })
})
