/**
 * The welcome screen's memory chip sits directly above the composer, and only
 * while the welcome state shows (empty session). WelcomeView is
 * mocked to nothing here, so any chip found comes from ChatPage's own slot.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer, { switchSlot } from '../store/chatSlice'
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
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { api } from '../api/client'
import { clearSlotSuccession } from '../utils/slotSuccession'
// The surface registry is populated by module side effect, and only `App.tsx`
// imports it in production -- so a harness that mounts ChatPage directly starts
// with an EMPTY registry and every surface lookup misses. Import it here for the
// same reason the app does. (The miss degrades safely to the surface-free
// sentence, which is what the unregistered-surface case below asserts.)
import '../surfaces/builtins'

type Slot = { messages: Msg[]; mode?: string; slotKeys?: string[] }

const pasteImage = (input: HTMLElement, file: File) =>
  fireEvent.paste(input, {
    clipboardData: {
      types: ['Files'],
      items: [{ kind: 'file', type: file.type, getAsFile: () => file }],
      getData: () => '',
    },
  })

function makeStore({ messages, mode = '', slotKeys = ['slot-a'] }: Slot) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
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

    await waitFor(() => expect(screen.getByTestId('action-error')).toHaveTextContent('Memory mode switch failed'))
    expect(deleteChatSlot).not.toHaveBeenCalled()
  })

  it('shows an error when deleting the old slot fails', async () => {
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    deleteChatSlot.mockRejectedValueOnce(new Error('Old session delete failed'))
    await renderWith({ messages: [] })

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)

    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-a'))
    // deleteSlot rethrows its own 'save failed' in place of the API error.
    await waitFor(() => expect(screen.getByTestId('action-error')).toHaveTextContent('save failed'))
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
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'))
    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-a'))
    await waitFor(() => expect(composer().value).toBe('half-written prompt'))

    // And back: the "switch to persistent" chip recreates the slot again.
    fireEvent.change(composer(), { target: { value: 'half-written prompt, more' } })
    fireEvent.click(screen.getByTestId('memory-mode-chip'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-c'))
    await waitFor(() => expect(composer().value).toBe('half-written prompt, more'))
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
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled())

    // The user opens another session while the create is still pending.
    await act(async () => { await store.dispatch(switchSlot('slot-x')) })
    expect(store.getState().chat.activeSlot).toBe('slot-x')

    await act(async () => { resolveCreate({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' }) })

    // The unused replacement is dropped; slot A (and its draft) survives and
    // the view stays where the user put it.
    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-b'))
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-a')
    expect(store.getState().chat.activeSlot).toBe('slot-x')
  })

  it('reports a failed cleanup of the abandoned replacement', async () => {
    let resolveCreate: (v: unknown) => void = () => {}
    createChatSlot.mockImplementationOnce(() => new Promise(r => { resolveCreate = r }))
    deleteChatSlot.mockRejectedValueOnce(new Error('cleanup failed'))
    const store = await renderWith({ messages: [], slotKeys: ['slot-a', 'slot-x'] })

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(createChatSlot).toHaveBeenCalled())
    await act(async () => { await store.dispatch(switchSlot('slot-x')) })
    await act(async () => { resolveCreate({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' }) })

    await waitFor(() => expect(screen.getByTestId('action-error')).toBeInTheDocument())
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
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'))

    // While slot-b's transcript is still loading, the user goes back to slot-a
    // and then on to a third session before the switch resolves.
    await act(async () => { await store.dispatch(switchSlot('slot-a')) })
    await act(async () => { await store.dispatch(switchSlot('slot-x')) })
    await act(async () => { releaseDetail() })

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-x'))
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-a')
    // The switch was abandoned, so its replacement must not linger.
    expect(deleteChatSlot).toHaveBeenCalledWith('slot-b')
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
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'))
    await act(async () => { failDetail() })

    // The rejected switch rolls the view back to slot-a; that slot change must
    // not wipe the only report that the switch failed.
    await waitFor(() => expect(screen.getByTestId('action-error')).toHaveTextContent('replacement unreachable'))
    expect(store.getState().chat.activeSlot).toBe('slot-a')
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })
    expect(screen.getByTestId('action-error')).toBeInTheDocument()
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-a')
    // The unused replacement is dropped rather than left as a stray empty session.
    expect(deleteChatSlot).toHaveBeenCalledWith('slot-b')
  })

  it('keeps the replacement and its draft when the user edits it, then goes back, while the switch loads', async () => {
    clearSlotSuccession()
    localStorage.clear()
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    deleteChatSlot.mockClear()
    const store = await renderWith({ messages: [] })
    const composer = () => screen.getAllByRole('textbox').at(-1)! as HTMLTextAreaElement
    let releaseDetail: () => void = () => {}
    vi.mocked(api.chatSlotDetail).mockImplementationOnce(() => new Promise(r => {
      releaseDetail = () => r({ messages: [], running: false, has_more: false, total: 0 } as never)
    }))

    fireEvent.change(composer(), { target: { value: 'carried' } })
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'))
    await waitFor(() => expect(composer().value).toBe('carried'))

    // Typed into the replacement while it loads, then back to slot-a.
    fireEvent.change(composer(), { target: { value: 'carried, typed during load' } })
    await act(async () => { await store.dispatch(switchSlot('slot-a')) })
    await act(async () => { releaseDetail() })
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })

    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-a')
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-b')
    // The edit is still in slot-b's draft when the user opens it again.
    await act(async () => { await store.dispatch(switchSlot('slot-b')) })
    await waitFor(() => expect(composer().value).toBe('carried, typed during load'))
  })

  it('keeps the replacement and its draft when the user edits it and the switch then 404s', async () => {
    clearSlotSuccession()
    localStorage.clear()
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    deleteChatSlot.mockClear()
    const store = await renderWith({ messages: [] })
    const composer = () => screen.getAllByRole('textbox').at(-1)! as HTMLTextAreaElement
    let failDetail: () => void = () => {}
    vi.mocked(api.chatSlotDetail).mockImplementationOnce(() => new Promise((_, reject) => {
      failDetail = () => reject(Object.assign(new Error('replacement unreachable'), { status: 404 }))
    }))

    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'))
    fireEvent.change(composer(), { target: { value: 'typed during load' } })
    await act(async () => { failDetail() })

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-a'))
    await act(async () => { await new Promise(r => setTimeout(r, 50)) })
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-a')
    expect(deleteChatSlot).not.toHaveBeenCalledWith('slot-b')
    await act(async () => { await store.dispatch(switchSlot('slot-b')) })
    await waitFor(() => expect(composer().value).toBe('typed during load'))
  })

  it('moves an upload that finished while the switch loaded into the replacement', async () => {
    // Earlier tests recorded slot-a -> slot-b -> slot-c in the module-level
    // succession table; start from a clean one.
    clearSlotSuccession()
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
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'))

    // The upload settles while slot-b is still loading: it lands in slot-a's
    // draft, which the switch is about to delete.
    await act(async () => { finishUpload() })
    await act(async () => { releaseDetail() })

    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-a'))
    await waitFor(() => expect(screen.getByRole('group', { name: '/uploads/during-switch.png' })).toBeInTheDocument())
  })

  it('lands an upload still running after the switch in the replacement', async () => {
    // Earlier tests recorded slot-a -> slot-b -> slot-c in the module-level
    // succession table; start from a clean one.
    clearSlotSuccession()
    localStorage.clear()
    deleteChatSlot.mockClear()
    createChatSlot.mockResolvedValueOnce({ key: 'slot-b', messages: 0, running: false, memory_mode: 'incognito' })
    let finishUpload: () => void = () => {}
    vi.mocked(api.uploadFiles).mockImplementationOnce(() => new Promise(r => {
      finishUpload = () => r({ paths: ['/uploads/after-switch.png'] } as never)
    }))
    const store = await renderWith({ messages: [] })

    await act(async () => { pasteImage(screen.getByLabelText('Message input'), new File(['px'], 'after-switch.png', { type: 'image/png' })) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())
    fireEvent.click(screen.getByText('Choose memory mode').closest('button')!)
    fireEvent.click(screen.getByText('Incognito').closest('button')!)
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-b'))
    await waitFor(() => expect(deleteChatSlot).toHaveBeenCalledWith('slot-a'))

    await act(async () => { finishUpload() })
    await waitFor(() => expect(screen.getByRole('group', { name: '/uploads/after-switch.png' })).toBeInTheDocument())
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
