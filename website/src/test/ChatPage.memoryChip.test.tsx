/**
 * The welcome screen's memory chip sits directly above the composer, and only
 * while the welcome state shows (empty session). WelcomeView is
 * mocked to nothing here, so any chip found comes from ChatPage's own slot.
 */
import { beforeEach, describe, it, expect, vi, onTestFinished } from 'vitest'
import { render, screen, act, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore, type Middleware } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer, { createSlot, switchSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))

type Msg = { role: string; content: string }
const detail = vi.hoisted(() => ({ messages: [] as Msg[] }))
const createChatSlot = vi.hoisted(() => vi.fn())
const deleteChatSlot = vi.hoisted(() => vi.fn().mockResolvedValue(undefined))
vi.mock('../api/client', () => ({
  api: {
    createChatSlot,
    deleteChatSlot,
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn(async () => ({ messages: detail.messages, running: false, has_more: false, total: detail.messages.length })),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
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
const switchTimeout = vi.hoisted(() => vi.fn(() => 15_000))
vi.mock('../utils/memorySwitchTimeout', () => ({ memorySwitchTimeoutMs: switchTimeout }))
// Every wait after a memory-mode pick crosses the lazy boundary
// `import('./chat/memoryModeSwitch')` in ChatPage, then the create and the
// switch, so it gets more than the 1000ms default.
const SWITCH_CHAIN_WAIT = { timeout: 5000 }
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { api } from '../api/client'
import { __resetComposerSendHoldsForTests, isComposerSendHeld, takeComposerArrivals } from '../utils/composerSendHolds'
// The surface registry is populated by module side effect, and only `App.tsx`
// imports it in production -- so a harness that mounts ChatPage directly starts
// with an EMPTY registry and every surface lookup misses. Import it here for the
// same reason the app does. (The miss degrades safely to the surface-free
// sentence, which is what the unregistered-surface case below asserts.)
import '../surfaces/builtins'

beforeEach(() => {
  createChatSlot.mockReset()
  deleteChatSlot.mockReset().mockResolvedValue(undefined)
  vi.mocked(api.chatSlotDetail).mockReset().mockImplementation(async () => ({
    messages: detail.messages,
    running: false,
    has_more: false,
    total: detail.messages.length,
  }))
  vi.mocked(api.uploadFiles).mockReset().mockResolvedValue({ paths: [] })
  switchTimeout.mockReset().mockReturnValue(15_000)
})

type Slot = { messages: Msg[]; mode?: string; slotKeys?: string[] }

const pasteImage = (input: HTMLElement, file: File) =>
  fireEvent.paste(input, {
    clipboardData: {
      types: ['Files'],
      items: [{ kind: 'file', type: file.type, getAsFile: () => file }],
      getData: () => '',
    },
  })

/** Slots whose `switchSlot` has settled (fulfilled or rejected), so a test can
 *  wait for the memory-mode switch to finish before asserting what it left alone. */
const settledSwitches: string[] = []
/** Replacement slots whose `createSlot` has fulfilled. */
const createdSlots: string[] = []
const recordSettledSwitch: Middleware = () => next => action => {
  if (switchSlot.fulfilled.match(action) || switchSlot.rejected.match(action)) {
    const arg = action.meta.arg
    settledSwitches.push(typeof arg === 'string' ? arg : arg.key)
  }
  if (createSlot.fulfilled.match(action) && action.payload?.key) createdSlots.push(action.payload.key)
  return next(action)
}

function makeStore({ messages, mode = '', slotKeys = ['slot-a'] }: Slot) {
  settledSwitches.length = 0
  createdSlots.length = 0
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    middleware: getDefault => getDefault().concat(recordSettledSwitch),
    preloadedState: {
      dashboard: {
        status: null,
        slots: slotKeys.map(key => ({ key, messages: key === 'slot-a' ? messages.length : 0, running: false, mode: key === 'slot-a' ? mode : '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages,
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        unresumableResume: null, lastResumeRequestId: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderWith(slot: Slot) {
  detail.messages = slot.messages
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slot)
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  return store
}

describe('memory chip above the composer', () => {
  it('renders above the composer on the welcome state', async () => {
    await renderWith({ messages: [] })
    const chip = screen.getByTestId('composer-memory-chip')
    expect(chip.textContent).toContain('Choose memory mode')
    const composer = screen.getAllByRole('textbox').at(-1)!
    // DOCUMENT_POSITION_FOLLOWING: the composer comes after the chip.
    expect(chip.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows an error when creating the replacement slot fails', async () => {
    createChatSlot.mockRejectedValueOnce(new Error('Memory mode switch failed'))
    await renderWith({ messages: [] })

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)

    await waitFor(() => expect(screen.getByTestId('action-error')).toHaveTextContent('Memory mode switch failed'), SWITCH_CHAIN_WAIT)
    expect(deleteChatSlot).not.toHaveBeenCalled()
  })

  it('shows an error when deleting the old slot fails', async () => {
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    deleteChatSlot.mockRejectedValueOnce(new Error('Old session delete failed'))
    await renderWith({ messages: [] })

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)

    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-a'), SWITCH_CHAIN_WAIT)
    // deleteSlot rethrows its own 'save failed' in place of the API error.
    await waitFor(() => expect(screen.getByTestId('action-error')).toHaveTextContent('save failed'), SWITCH_CHAIN_WAIT)
  })

  it('keeps the unsent draft when switching memory mode, both directions', async () => {
    createChatSlot
      .mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
      .mockResolvedValueOnce({ key: 'slot-c', messages: 0, running: false, memory_mode: 'persistent' })
    const store = await renderWith({ messages: [] })
    const composer = () => screen.getAllByRole('textbox').at(-1)! as HTMLTextAreaElement

    fireEvent.change(composer(), { target: { value: 'half-written prompt' } })
    expect(composer().value).toBe('half-written prompt')

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)
    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-a'), SWITCH_CHAIN_WAIT)
    await waitFor(() => expect(composer().value).toBe('half-written prompt'), SWITCH_CHAIN_WAIT)

    // And back: the "switch to persistent" chip recreates the slot again.
    fireEvent.change(composer(), { target: { value: 'half-written prompt, more' } })
    fireEvent.click(screen.getByTestId('memory-mode-chip'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-c'), SWITCH_CHAIN_WAIT)
    await waitFor(() => expect(composer().value).toBe('half-written prompt, more'), SWITCH_CHAIN_WAIT)
  })

  it('holds the switch while an optimize runs, then switches with the rewrite', async () => {
    let releaseOptimize: () => void = () => {}
    const optimizeDone = new Promise<void>(r => { releaseOptimize = r })
    const realFetch = globalThis.fetch
    vi.stubGlobal('fetch', (url: string) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        return optimizeDone.then(() => ({ ok: true, json: async () => ({ changed: true, optimized: 'rewritten prompt' }) }))
      }
      return Promise.resolve({ ok: true, json: async () => [] } as unknown as Response)
    })
    try {
      createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
      const store = await renderWith({ messages: [] })
      const composer = () => screen.getAllByRole('textbox').at(-1)! as HTMLTextAreaElement
      fireEvent.change(composer(), { target: { value: 'rough prompt' } })
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' })) })

      // While the optimize runs the chip is inert: a switch now would carry
      // 'rough prompt' and send the rewrite to the session it deletes.
      await waitFor(() => expect(screen.getByTestId('memory-mode-chip')).toBeDisabled())
      expect(screen.getByTestId('memory-mode-chip')).toHaveTextContent('Wait for the prompt optimizer to finish')
      const createsBefore = createChatSlot.mock.calls.length
      fireEvent.click(screen.getByTestId('memory-mode-chip'))
      expect(screen.queryByText('Incognito')).toBeNull()
      expect(createChatSlot.mock.calls.length).toBe(createsBefore)

      await act(async () => { releaseOptimize(); await optimizeDone })
      await waitFor(() => expect(composer().value).toBe('rewritten prompt'))
      await waitFor(() => expect(screen.getByTestId('memory-mode-chip')).not.toBeDisabled())

      fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
      fireEvent.click(screen.getByText('Incognito').closest('button')!)
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)
      await waitFor(() => expect(composer().value).toBe('rewritten prompt'), SWITCH_CHAIN_WAIT)
    } finally {
      vi.stubGlobal('fetch', realFetch)
    }
  })

  it('abandons the switch, keeping the old session, when the user navigates away mid-create', async () => {
    let resolveCreate: (v: unknown) => void = () => {}
    createChatSlot.mockImplementationOnce(() => new Promise(r => { resolveCreate = r }))
    deleteChatSlot.mockClear()
    const store = await renderWith({ messages: [], slotKeys: ['slot-a', 'slot-x'] })
    const composer = () => screen.getAllByRole('textbox').at(-1)! as HTMLTextAreaElement

    fireEvent.change(composer(), { target: { value: 'draft in A' } })
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)

    // The user opens another session while the create is still pending.
    await act(async () => { await store.dispatch(switchSlot('slot-x')) })
    expect(store.getState().chat.activeSlot).toBe('slot-x')

    await act(async () => { resolveCreate({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' }) })

    // The unused replacement is dropped; slot A (and its draft) survives and
    // the view stays where the user put it.
    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-b'), SWITCH_CHAIN_WAIT)
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-a')
    expect(store.getState().chat.activeSlot).toBe('slot-x')
  })

  it('abandons the switch, keeping the old session, when the user goes away and back mid-create', async () => {
    let resolveCreate: (v: unknown) => void = () => {}
    createChatSlot.mockImplementationOnce(() => new Promise(r => { resolveCreate = r }))
    deleteChatSlot.mockClear()
    const store = await renderWith({ messages: [], slotKeys: ['slot-a', 'slot-x'] })

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)

    // The user opens another session and returns to A before the create ends.
    await act(async () => { await store.dispatch(switchSlot('slot-x')) })
    await act(async () => { await store.dispatch(switchSlot('slot-a')) })

    await act(async () => { resolveCreate({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' }) })

    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-b'), SWITCH_CHAIN_WAIT)
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-a')
    expect(settledSwitches).not.toContain('slot-b')
    expect(store.getState().chat.activeSlot).toBe('slot-a')
  })

  it('reports a failed cleanup of the abandoned replacement', async () => {
    let resolveCreate: (v: unknown) => void = () => {}
    createChatSlot.mockImplementationOnce(() => new Promise(r => { resolveCreate = r }))
    deleteChatSlot.mockRejectedValueOnce(new Error('cleanup failed'))
    const store = await renderWith({ messages: [], slotKeys: ['slot-a', 'slot-x'] })

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)
    await act(async () => { await store.dispatch(switchSlot('slot-x')) })
    await act(async () => { resolveCreate({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' }) })

    await waitFor(() => expect(screen.getByTestId('action-error')).toBeInTheDocument(), SWITCH_CHAIN_WAIT)
  })

  it('keeps the replacement when the user opens it while the create is pending', async () => {
    let resolveCreate: (v: unknown) => void = () => {}
    createChatSlot.mockImplementationOnce(() => new Promise(r => { resolveCreate = r }))
    deleteChatSlot.mockClear()
    const store = await renderWith({ messages: [], slotKeys: ['slot-a', 'slot-x', 'slot-b'] })

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)
    // The user leaves, then opens the replacement as soon as the slots
    // broadcast lists it (seeded above as slot-b), before the create returns.
    await act(async () => { await store.dispatch(switchSlot('slot-x')) })
    await act(async () => { await store.dispatch(switchSlot('slot-b')) })
    await act(async () => { resolveCreate({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' }) })

    expect(store.getState().chat.activeSlot).toBe('slot-b')
    expect(deleteChatSlot).not.toHaveBeenCalled()
  })

  it('locks the composer in every session while the create runs, so nothing can be typed or pasted', async () => {
    let resolveCreate: (v: unknown) => void = () => {}
    createChatSlot.mockImplementationOnce(() => new Promise(r => { resolveCreate = r }))
    deleteChatSlot.mockClear()
    vi.mocked(api.uploadFiles).mockClear()
    localStorage.clear()
    const store = await renderWith({ messages: [], slotKeys: ['slot-a', 'slot-x', 'slot-b'] })
    const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)
    expect(screen.getByTestId('memory-switch-status')).toHaveTextContent('Switching memory mode')
    expect(screen.getByTestId('memory-mode-chip')).toBeDisabled()
    expect(screen.getByTestId('memory-mode-chip')).toHaveAttribute('aria-busy', 'true')

    // The replacement shows up in the list before the create returns; the
    // user opens it and tries to type and paste there.
    await act(async () => { await store.dispatch(switchSlot('slot-x')) })
    await act(async () => { await store.dispatch(switchSlot('slot-b')) })
    expect(composer()).toHaveAttribute('aria-disabled', 'true')
    expect(composer()).toHaveAttribute('readonly')
    expect(composer()).toHaveAttribute('placeholder', 'Switching memory mode…')
    await act(async () => { pasteImage(composer(), new File(['px'], 'mid-switch.png', { type: 'image/png' })) })
    expect(api.uploadFiles).not.toHaveBeenCalled()

    await act(async () => { resolveCreate({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' }) })
    await waitFor(() => expect(composer()).not.toHaveAttribute('aria-disabled'), SWITCH_CHAIN_WAIT)
    expect(screen.getByTestId('memory-switch-status')).toHaveTextContent('')
    expect(deleteChatSlot).not.toHaveBeenCalled()
  })

  it('unlocks, keeps both sessions and reports it when the create does not settle in time', async () => {
    let resolveCreate: (v: unknown) => void = () => {}
    createChatSlot.mockImplementationOnce(() => new Promise(r => { resolveCreate = r }))
    deleteChatSlot.mockClear()
    localStorage.clear()
    // The create is held pending, so only the timeout can end the lock.
    switchTimeout.mockReturnValueOnce(1)
    const store = await renderWith({ messages: [] })
    const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.change(composer(), { target: { value: 'draft in A' } })
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)

    await waitFor(() => expect(screen.getByTestId('action-error')).toHaveTextContent('taking too long'), SWITCH_CHAIN_WAIT)
    expect(composer()).not.toHaveAttribute('aria-disabled')
    expect(screen.getByTestId('memory-mode-chip')).not.toBeDisabled()
    fireEvent.change(composer(), { target: { value: 'draft in A, typed after the timeout' } })

    // The late create must not carry or delete anything.
    await act(async () => { resolveCreate({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' }) })
    await waitFor(() => expect(createdSlots).toContain('slot-b'), SWITCH_CHAIN_WAIT)
    expect(deleteChatSlot).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe('slot-a')
    expect(composer().value).toBe('draft in A, typed after the timeout')
    expect(JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')['slot-b']).toBeUndefined()
  })

  it('keeps both sessions, carrying nothing, when the page unmounts while the create runs', async () => {
    let resolveCreate: (v: unknown) => void = () => {}
    createChatSlot.mockImplementationOnce(() => new Promise(r => { resolveCreate = r }))
    deleteChatSlot.mockClear()
    localStorage.clear()
    const store = await renderWith({ messages: [] })

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'draft in A' } })
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)
    // The user leaves the chat page; the detached page still holds A's refs.
    cleanup()
    await act(async () => { resolveCreate({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' }) })
    await waitFor(() => expect(createdSlots).toContain('slot-b'), SWITCH_CHAIN_WAIT)

    expect(deleteChatSlot).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe('slot-a')
    expect(JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')['slot-b']).toBeUndefined()
  })

  it('keeps the old session when the user returns to it while the switch is loading', async () => {
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    deleteChatSlot.mockClear()
    const store = await renderWith({ messages: [], slotKeys: ['slot-a', 'slot-x'] })
    // Hold slot-b's transcript fetch so the switch stays in flight.
    let releaseDetail: () => void = () => {}
    vi.mocked(api.chatSlotDetail).mockImplementationOnce(() => new Promise(r => {
      releaseDetail = () => r({ messages: [], running: false, has_more: false, total: 0 } as never)
    }))

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)

    // While slot-b's transcript is still loading, the user goes back to slot-a
    // and then on to a third session before the switch resolves.
    await act(async () => { await store.dispatch(switchSlot('slot-a')) })
    await act(async () => { await store.dispatch(switchSlot('slot-x')) })
    await act(async () => { releaseDetail() })

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-x'), SWITCH_CHAIN_WAIT)
    // The switch was abandoned, so its replacement must not linger.
    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-b'), SWITCH_CHAIN_WAIT)
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-a')
  })

  it('keeps the failure notice when the switch to the replacement is rejected', async () => {
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    deleteChatSlot.mockClear()
    const store = await renderWith({ messages: [] })
    // A 404 on the replacement: switchSlot rejects and rolls the view back.
    let failDetail: () => void = () => {}
    vi.mocked(api.chatSlotDetail).mockImplementationOnce(() => new Promise((_, reject) => {
      failDetail = () => reject(Object.assign(new Error('replacement unreachable'), { status: 404 }))
    }))

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)
    await act(async () => { failDetail() })

    // The rejected switch rolls the view back to slot-a; that slot change must
    // not wipe the only report that the switch failed.
    await waitFor(() => expect(screen.getByTestId('action-error')).toHaveTextContent('replacement unreachable'), SWITCH_CHAIN_WAIT)
    expect(store.getState().chat.activeSlot).toBe('slot-a')
    // The unused replacement is dropped rather than left as a stray empty session.
    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-b'), SWITCH_CHAIN_WAIT)
    expect(screen.getByTestId('action-error')).toBeInTheDocument()
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-a')
    expect(screen.getByLabelText('Message input')).not.toHaveAttribute('aria-disabled')
  })

  it('keeps the replacement the user typed in when the switch rejects after the timeout', async () => {
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    deleteChatSlot.mockClear()
    localStorage.clear()
    // The switch's timeout is captured and fired by hand once the switch is in
    // flight, so the test never races a real timer.
    const TIMEOUT_SENTINEL = 987_654
    switchTimeout.mockReturnValueOnce(TIMEOUT_SENTINEL)
    const realSetTimeout = window.setTimeout
    let fireTimeout: () => void = () => {}
    const timerSpy = vi.spyOn(window, 'setTimeout').mockImplementation(((fn: () => void, ms?: number, ...rest: unknown[]) => {
      if (ms === TIMEOUT_SENTINEL) { fireTimeout = fn; return 0 }
      return realSetTimeout(fn, ms, ...rest)
    }) as typeof window.setTimeout)
    onTestFinished(() => timerSpy.mockRestore())
    const store = await renderWith({ messages: [], slotKeys: ['slot-a', 'slot-x'] })
    const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    // Hold slot-b's transcript fetch past the timeout, then reject it.
    let failDetail: () => void = () => {}
    vi.mocked(api.chatSlotDetail).mockImplementationOnce(() => new Promise((_, reject) => {
      failDetail = () => reject(Object.assign(new Error('replacement unreachable'), { status: 404 }))
    }))

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)
    await act(async () => { fireTimeout() })
    await waitFor(() => expect(screen.getByTestId('action-error')).toHaveTextContent('taking too long'), SWITCH_CHAIN_WAIT)
    // Unlocked: the user types in the replacement, then opens another session.
    await waitFor(() => expect(composer()).not.toHaveAttribute('aria-disabled'), SWITCH_CHAIN_WAIT)
    fireEvent.change(composer(), { target: { value: 'typed in B after the timeout' } })
    await act(async () => { await store.dispatch(switchSlot('slot-x')) })
    await act(async () => { failDetail() })

    await waitFor(() => expect(settledSwitches).toContain('slot-b'), SWITCH_CHAIN_WAIT)
    // The rejection is reported; the cleanup it skipped would have run in the same branch.
    await waitFor(() => expect(screen.getByTestId('action-error')).toHaveTextContent('replacement unreachable'), SWITCH_CHAIN_WAIT)
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-b')
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-a')
  })

  it('keeps the composer locked while the switch loads, then unlocks it in the replacement with the draft', async () => {
    __resetComposerSendHoldsForTests()
    localStorage.clear()
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    deleteChatSlot.mockClear()
    const store = await renderWith({ messages: [] })
    const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    let releaseDetail: () => void = () => {}
    vi.mocked(api.chatSlotDetail).mockImplementationOnce(() => new Promise(r => {
      releaseDetail = () => r({ messages: [], running: false, has_more: false, total: 0 } as never)
    }))

    fireEvent.change(composer(), { target: { value: 'carried' } })
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)
    await waitFor(() => expect(composer().value).toBe('carried'), SWITCH_CHAIN_WAIT)
    // In the replacement while its transcript loads: still locked.
    expect(composer()).toHaveAttribute('aria-disabled', 'true')

    await act(async () => { releaseDetail() })
    await waitFor(() => expect(settledSwitches).toContain('slot-b'), SWITCH_CHAIN_WAIT)
    await waitFor(() => expect(composer()).not.toHaveAttribute('aria-disabled'), SWITCH_CHAIN_WAIT)
    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-a'), SWITCH_CHAIN_WAIT)
    expect(composer().value).toBe('carried')
  })

  it('moves an upload that finished while the switch loaded into the replacement', async () => {
    __resetComposerSendHoldsForTests()
    localStorage.clear()
    deleteChatSlot.mockClear()
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    let finishUpload: () => void = () => {}
    vi.mocked(api.uploadFiles).mockImplementationOnce(() => new Promise(r => {
      finishUpload = () => r({ paths: ['/uploads/during-switch.png'] } as never)
    }))
    const store = await renderWith({ messages: [] })
    // Hold slot-b's transcript fetch so the switch stays in flight.
    let releaseDetail: () => void = () => {}
    vi.mocked(api.chatSlotDetail).mockImplementationOnce(() => new Promise(r => {
      releaseDetail = () => r({ messages: [], running: false, has_more: false, total: 0 } as never)
    }))

    await act(async () => { pasteImage(screen.getByLabelText('Message input'), new File(['px'], 'during-switch.png', { type: 'image/png' })) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)

    // The upload settles while slot-b is still loading: it lands in slot-a's
    // draft, which the switch is about to delete.
    await act(async () => { finishUpload() })
    await act(async () => { releaseDetail() })

    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-a'), SWITCH_CHAIN_WAIT)
    await waitFor(() => expect(screen.getByRole('group', { name: '/uploads/during-switch.png' })).toBeInTheDocument(), SWITCH_CHAIN_WAIT)
  })

  it('keeps both sessions, and the upload in the old one, when the page unmounts while the switch loads', async () => {
    localStorage.clear()
    __resetComposerSendHoldsForTests()
    deleteChatSlot.mockClear()
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    let finishUpload: () => void = () => {}
    vi.mocked(api.uploadFiles).mockImplementationOnce(() => new Promise(r => {
      finishUpload = () => r({ paths: ['/uploads/after-unmount.png'] } as never)
    }))
    const store = await renderWith({ messages: [] })
    let releaseDetail: () => void = () => {}
    vi.mocked(api.chatSlotDetail).mockImplementationOnce(() => new Promise(r => {
      releaseDetail = () => r({ messages: [], running: false, has_more: false, total: 0 } as never)
    }))

    await act(async () => { pasteImage(screen.getByLabelText('Message input'), new File(['px'], 'after-unmount.png', { type: 'image/png' })) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)

    // The user leaves the chat page while slot-b loads; the upload lands meanwhile.
    cleanup()
    await act(async () => { finishUpload() })
    await act(async () => { releaseDetail() })
    await waitFor(() => expect(settledSwitches).toContain('slot-b'), SWITCH_CHAIN_WAIT)

    expect(deleteChatSlot).not.toHaveBeenCalled()
    expect(takeComposerArrivals('slot-a')).toEqual(['/uploads/after-unmount.png'])
    expect(takeComposerArrivals('slot-b')).toEqual([])
  })

  it('holds Send in the replacement while an upload from the old session is still running', async () => {
    localStorage.clear()
    __resetComposerSendHoldsForTests()
    deleteChatSlot.mockClear()
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    let finishUpload: () => void = () => {}
    vi.mocked(api.uploadFiles).mockImplementationOnce(() => new Promise(r => {
      finishUpload = () => r({ paths: ['/uploads/held.png'] } as never)
    }))
    const store = await renderWith({ messages: [] })

    await act(async () => { pasteImage(screen.getByLabelText('Message input'), new File(['px'], 'held.png', { type: 'image/png' })) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)
    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-a'), SWITCH_CHAIN_WAIT)

    expect(isComposerSendHeld('slot-b')).toBe(true)
    await act(async () => { finishUpload() })
    await waitFor(() => expect(screen.getByRole('group', { name: '/uploads/held.png' })).toBeInTheDocument(), SWITCH_CHAIN_WAIT)
    expect(isComposerSendHeld('slot-b')).toBe(false)
    expect(isComposerSendHeld('slot-a')).toBe(false)
  })

  it('releases Send in the replacement when the old session delete fails mid-upload', async () => {
    localStorage.clear()
    __resetComposerSendHoldsForTests()
    deleteChatSlot.mockClear()
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    deleteChatSlot.mockRejectedValueOnce(new Error('Old session delete failed'))
    let finishUpload: () => void = () => {}
    vi.mocked(api.uploadFiles).mockImplementationOnce(() => new Promise(r => {
      finishUpload = () => r({ paths: ['/uploads/delete-failed.png'] } as never)
    }))
    const store = await renderWith({ messages: [] })

    await act(async () => { pasteImage(screen.getByLabelText('Message input'), new File(['px'], 'delete-failed.png', { type: 'image/png' })) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)
    await waitFor(() => expect(screen.getByTestId('action-error')).toHaveTextContent('save failed'), SWITCH_CHAIN_WAIT)

    // The hold moved to the replacement with the switch; the upload must
    // settle there too, or Send stays held in the session the user is in.
    expect(isComposerSendHeld('slot-b')).toBe(true)
    await act(async () => { finishUpload() })
    await waitFor(() => expect(screen.getByRole('group', { name: '/uploads/delete-failed.png' })).toBeInTheDocument(), SWITCH_CHAIN_WAIT)
    expect(isComposerSendHeld('slot-b')).toBe(false)
    expect(isComposerSendHeld('slot-a')).toBe(false)
  })

  it('keeps a later upload in the old session when its delete fails', async () => {
    localStorage.clear()
    __resetComposerSendHoldsForTests()
    deleteChatSlot.mockClear()
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    deleteChatSlot.mockRejectedValueOnce(new Error('Old session delete failed'))
    vi.mocked(api.uploadFiles).mockClear()
    let finishFirst: () => void = () => {}
    let finishLater: () => void = () => {}
    vi.mocked(api.uploadFiles)
      .mockImplementationOnce(() => new Promise(r => { finishFirst = () => r({ paths: ['/uploads/in-flight.png'] } as never) }))
      .mockImplementationOnce(() => new Promise(r => { finishLater = () => r({ paths: ['/uploads/later.png'] } as never) }))
    const store = await renderWith({ messages: [] })

    await act(async () => { pasteImage(screen.getByLabelText('Message input'), new File(['px'], 'in-flight.png', { type: 'image/png' })) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1), SWITCH_CHAIN_WAIT)
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)
    await waitFor(() => expect(screen.getByTestId('action-error')).toHaveTextContent('save failed'), SWITCH_CHAIN_WAIT)
    await act(async () => { finishFirst() })
    await waitFor(() => expect(screen.getByRole('group', { name: '/uploads/in-flight.png' })).toBeInTheDocument(), SWITCH_CHAIN_WAIT)

    // The old session survived its failed delete. The user goes back to it
    // and uploads there: that upload belongs to the old session, not the
    // replacement.
    await act(async () => { await store.dispatch(switchSlot('slot-a')) })
    await act(async () => { pasteImage(screen.getByLabelText('Message input'), new File(['px'], 'later.png', { type: 'image/png' })) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(2), SWITCH_CHAIN_WAIT)
    expect(isComposerSendHeld('slot-a')).toBe(true)
    await act(async () => { finishLater() })

    await waitFor(() => expect(screen.getByRole('group', { name: '/uploads/later.png' })).toBeInTheDocument(), SWITCH_CHAIN_WAIT)
    expect(isComposerSendHeld('slot-a')).toBe(false)
    expect(isComposerSendHeld('slot-b')).toBe(false)
    expect(takeComposerArrivals('slot-b')).toEqual([])
  })

  it('lands an upload still running after the switch in the replacement', async () => {
    __resetComposerSendHoldsForTests()
    localStorage.clear()
    deleteChatSlot.mockClear()
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    let finishUpload: () => void = () => {}
    vi.mocked(api.uploadFiles).mockImplementationOnce(() => new Promise(r => {
      finishUpload = () => r({ paths: ['/uploads/after-switch.png'] } as never)
    }))
    const store = await renderWith({ messages: [] })

    await act(async () => { pasteImage(screen.getByLabelText('Message input'), new File(['px'], 'after-switch.png', { type: 'image/png' })) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled(), SWITCH_CHAIN_WAIT)
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'), SWITCH_CHAIN_WAIT)
    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-a'), SWITCH_CHAIN_WAIT)

    await act(async () => { finishUpload() })
    await waitFor(() => expect(screen.getByRole('group', { name: '/uploads/after-switch.png' })).toBeInTheDocument(), SWITCH_CHAIN_WAIT)
  })

  it('is absent once the session has messages', async () => {
    await renderWith({ messages: [{ role: 'user', content: 'hello' }, { role: 'assistant', content: 'hi' }] })
    expect(screen.queryByTestId('composer-memory-chip')).toBeNull()
  })

  it('still renders for a slot carrying the legacy orchestrator mode', async () => {
    await renderWith({ messages: [], mode: 'orchestrator' })
    expect(await screen.findByTestId('composer-memory-chip')).toBeInTheDocument()
  })
})
