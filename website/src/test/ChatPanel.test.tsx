/**
 * The app SDK ChatPanel mounts ChatPage for the session the app hands it. It
 * adds a row for a session the slot list has not caught up with yet, and shows
 * the chat only once that session is open and active: a session that is gone
 * or fails to open never puts another conversation in its place (#25341).
 * ChatPage is stubbed, so a rendered chat is the active slot on screen.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { addSlotOptimistic, removeSlotOptimistic } from '../store/dashboardSlice'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'
import { i18nT } from '../i18n/t'
import { clearSlotSuccession, recordSlotSuccession } from '../utils/slotSuccession'
import ChatPanel from '../app-sdk/ChatPanel'

vi.mock('../pages/ChatPage', async () => {
  const { useContext } = await import('react')
  const { ExpandStepsContext } = await import('../hooks/useToolCallsStartExpanded')
  function ChatPageStub(props: { embedded?: boolean; embedMode?: 'chat' | 'sessions'; noUrlSync?: boolean }) {
    const expandSteps = useContext(ExpandStepsContext)
    return (
      <div
        data-testid="chat-page"
        data-embedded={props.embedded ? 'true' : 'false'}
        data-embed-mode={props.embedMode ?? 'unset'}
        data-no-url-sync={props.noUrlSync === undefined ? 'unset' : String(props.noUrlSync)}
        data-expand-steps={String(expandSteps)}
      >
        ChatPage
      </div>
    )
  }
  return { default: ChatPageStub }
})

// Mirrors the real thunk: a live session becomes the active slot; a deleted one
// rejects with the 404 payload and leaves the previous active slot in place; a
// held one opens only when the test calls the function stored for it.
// Records whether the slot list had the row when the switch ran.
const goneSlots = vi.hoisted(() => new Set<string>())
const heldSlots = vi.hoisted(() => new Map<string, () => void>())
const listedAtSwitch = vi.hoisted(() => [] as boolean[])
const switchSlot = vi.hoisted(() => vi.fn((key: string) => (dispatch: (a: unknown) => void, getState: () => RootState) => {
  listedAtSwitch.push(getState().dashboard.slots.some(s => s.key === key))
  if (goneSlots.has(key)) return { unwrap: () => Promise.reject({ status: 404, message: 'not found' }) }
  if (heldSlots.has(key)) {
    const held = new Promise<void>(resolve => {
      heldSlots.set(key, () => { dispatch({ type: 'chat/setActiveSlot', payload: key }); resolve() })
    })
    return { unwrap: () => held }
  }
  dispatch({ type: 'chat/setActiveSlot', payload: key })
  return { unwrap: () => Promise.resolve() }
}))
vi.mock('../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  switchSlot,
}))

const slot = (key: string) => ({ key, title: key, messages: 0, running: false }) as ChatSlot

// The dashboard has another chat open; `keys` are listed too.
function storeWith(...keys: string[]) {
  const store = createTestStore()
  act(() => {
    for (const key of ['zzq-other', ...keys]) store.dispatch(addSlotOptimistic(slot(key)))
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-other' })
  })
  return store
}

const switchesTo = (key: string) => switchSlot.mock.calls.filter(([k]) => k === key).length

// The rows the panel itself adds, read off the store's dispatch.
function placeholdersAdded(store: ReturnType<typeof createTestStore>) {
  const spy = vi.spyOn(store, 'dispatch')
  return () => spy.mock.calls
    .map(([action]) => action as { type?: string; payload?: { key?: string } })
    .filter(a => a.type === addSlotOptimistic.type)
    .map(a => a.payload?.key)
}

beforeEach(() => {
  switchSlot.mockClear()
  listedAtSwitch.length = 0
  clearSlotSuccession()
  goneSlots.clear()
  heldSlots.clear()
})

describe('ChatPanel', () => {
  it('adds a slot missing from the slot list before switching to it', async () => {
    const store = storeWith()
    const added = placeholdersAdded(store)
    renderWithProviders(<ChatPanel slotKey="zzq-new" />, { store })
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument()
    expect(added()).toEqual(['zzq-new'])
    expect(listedAtSwitch).toEqual([true])
  })

  it('does not add a slot the slot list already has', async () => {
    const store = storeWith('zzq-slot-1')
    const added = placeholdersAdded(store)
    renderWithProviders(<ChatPanel slotKey="zzq-slot-1" />, { store })
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument()
    expect(added()).toEqual([])
    expect(switchesTo('zzq-slot-1')).toBe(1)
  })

  it('renders ChatPage with embedded prop and preserves the default mode', async () => {
    renderWithProviders(<ChatPanel slotKey="zzq-slot-1" />, { store: storeWith('zzq-slot-1') })
    const chatPage = await screen.findByTestId('chat-page')
    expect(chatPage.getAttribute('data-embedded')).toBe('true')
    expect(chatPage.getAttribute('data-embed-mode')).toBe('unset')
    expect(chatPage.getAttribute('data-no-url-sync')).toBe('unset')
  })

  it('maps conversationOnly to the native chat embed without URL sync', async () => {
    renderWithProviders(<ChatPanel slotKey="zzq-slot-1" conversationOnly />, { store: storeWith('zzq-slot-1') })
    const chatPage = await screen.findByTestId('chat-page')
    expect(chatPage.getAttribute('data-embedded')).toBe('true')
    expect(chatPage.getAttribute('data-embed-mode')).toBe('chat')
    expect(chatPage.getAttribute('data-no-url-sync')).toBe('true')
  })

  it('opens the transcript steps for this panel only with expandSteps, writing no chat config', async () => {
    const { unmount } = renderWithProviders(<ChatPanel slotKey="zzq-slot-1" />, { store: storeWith('zzq-slot-1') })
    expect((await screen.findByTestId('chat-page')).getAttribute('data-expand-steps')).toBe('false')
    unmount()
    renderWithProviders(<ChatPanel slotKey="zzq-slot-1" expandSteps />, { store: storeWith('zzq-slot-1') })
    expect((await screen.findByTestId('chat-page')).getAttribute('data-expand-steps')).toBe('true')
    expect(localStorage.getItem('mc-chat-config')).toBeNull()
  })

  it('switches once per slot key, and again when the key changes', async () => {
    const { rerender } = renderWithProviders(<ChatPanel slotKey="zzq-slot-1" />, { store: storeWith('zzq-slot-1', 'zzq-slot-2') })
    await screen.findByTestId('chat-page')
    rerender(<ChatPanel slotKey="zzq-slot-1" />)
    expect(switchesTo('zzq-slot-1')).toBe(1)
    rerender(<ChatPanel slotKey="zzq-slot-2" />)
    await screen.findByTestId('chat-page')
    expect(switchesTo('zzq-slot-2')).toBe(1)
  })

  it('does not switch for an empty slot key', () => {
    renderWithProviders(<ChatPanel slotKey="" />, { store: storeWith() })
    expect(switchSlot).not.toHaveBeenCalled()
    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
  })

  it('wraps ChatPage in a flex container', () => {
    const { container } = renderWithProviders(<ChatPanel slotKey="zzq-slot-1" />, { store: storeWith('zzq-slot-1') })
    const wrapper = container.firstElementChild as HTMLElement
    expect(wrapper.className).toContain('flex')
    expect(wrapper.className).toContain('flex-col')
    expect(wrapper.className).toContain('h-full')
  })

  it('says a deleted session is gone instead of showing the open chat', async () => {
    goneSlots.add('zzq-gone')
    renderWithProviders(<ChatPanel slotKey="zzq-gone" conversationOnly />, { store: storeWith('zzq-gone') })
    expect(await screen.findByText(i18nT('appSdk.chatPanel.session_gone'))).toBeInTheDocument()
    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    expect(switchesTo('zzq-gone')).toBe(1)
  })

  it('follows a memory-mode replacement of a conversation-only session', async () => {
    const store = storeWith('zzq-slot-1')
    renderWithProviders(<ChatPanel slotKey="zzq-slot-1" conversationOnly />, { store })
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument()
    act(() => {
      store.dispatch(addSlotOptimistic(slot('zzq-slot-2')))
      recordSlotSuccession('zzq-slot-1', 'zzq-slot-2')
      store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-slot-2' })
    })
    act(() => { store.dispatch(removeSlotOptimistic('zzq-slot-1')) })
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
    expect(store.getState().chat.activeSlot).toBe('zzq-slot-2')
    expect(switchesTo('zzq-slot-1')).toBe(1)
  })

  it('with its sessions rail, keeps showing a session the user picks there', async () => {
    const store = storeWith('zzq-slot-1')
    renderWithProviders(<ChatPanel slotKey="zzq-slot-1" />, { store })
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument()
    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-other' }) })
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
    expect(store.getState().chat.activeSlot).toBe('zzq-other')
    expect(switchesTo('zzq-slot-1')).toBe(1)
  })

  it('with its sessions rail, draws no chat until its session has opened', async () => {
    heldSlots.set('zzq-slot-1', () => {})
    renderWithProviders(<ChatPanel slotKey="zzq-slot-1" />, { store: storeWith('zzq-slot-1') })
    await waitFor(() => expect(switchesTo('zzq-slot-1')).toBe(1))
    // The other chat is still the active slot, so a chat drawn now would be it.
    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    await act(async () => { heldSlots.get('zzq-slot-1')?.() })
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument()
  })

  it('stops taking its session back from a second conversation-only panel', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    onTestFinished(() => { warn.mockRestore(); vi.useRealTimers() })
    // Every switch-back lands at this one instant, so they form one quick run.
    vi.setSystemTime(new Date('2026-11-09T12:00:00Z'))
    const store = storeWith('zzq-slot-1', 'zzq-slot-2')
    renderWithProviders(
      <>
        <ChatPanel slotKey="zzq-slot-1" conversationOnly />
        <ChatPanel slotKey="zzq-slot-2" conversationOnly />
      </>,
      { store },
    )
    // The first panel gives up and draws nothing; the second keeps its session.
    await waitFor(() => {
      expect(warn).toHaveBeenCalledTimes(1)
      expect(screen.getAllByTestId('chat-page')).toHaveLength(1)
    })
    expect(store.getState().chat.activeSlot).toBe('zzq-slot-2')
    // Each panel: its first switch, then ten switch-backs.
    expect([switchesTo('zzq-slot-1'), switchesTo('zzq-slot-2')]).toEqual([11, 11])
    // A slot list change after a quiet gap does not start the fight again.
    vi.setSystemTime(new Date('2026-11-09T12:00:06Z'))
    await act(async () => { store.dispatch(addSlotOptimistic(slot('zzq-slot-3'))) })
    expect([switchesTo('zzq-slot-1'), switchesTo('zzq-slot-2')]).toEqual([11, 11])
    expect(store.getState().chat.activeSlot).toBe('zzq-slot-2')
    expect(warn).toHaveBeenCalledTimes(1)
  })

  it('takes its session back again once it has been handed another session', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    onTestFinished(() => { warn.mockRestore(); vi.useRealTimers() })
    // One instant throughout, so no quiet gap can be what starts a new run.
    vi.setSystemTime(new Date('2026-11-09T12:00:00Z'))
    const store = storeWith('zzq-slot-1', 'zzq-slot-2')
    const { rerender } = renderWithProviders(
      <>
        <ChatPanel key="a" slotKey="zzq-slot-1" conversationOnly />
        <ChatPanel key="b" slotKey="zzq-slot-2" conversationOnly />
      </>,
      { store },
    )
    await waitFor(() => expect(warn).toHaveBeenCalledTimes(1))
    expect(store.getState().chat.activeSlot).toBe('zzq-slot-2')
    // The competing panel goes, and the capped one is handed the session that is
    // already active, then its own again.
    rerender(<><ChatPanel key="a" slotKey="zzq-slot-1" conversationOnly /></>)
    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    rerender(<><ChatPanel key="a" slotKey="zzq-slot-2" conversationOnly /></>)
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument()
    rerender(<><ChatPanel key="a" slotKey="zzq-slot-1" conversationOnly /></>)
    await waitFor(() => {
      expect(store.getState().chat.activeSlot).toBe('zzq-slot-1')
      expect(screen.getByTestId('chat-page')).toBeInTheDocument()
    })
    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-other' }) })
    await waitFor(() => {
      expect(store.getState().chat.activeSlot).toBe('zzq-slot-1')
      expect(screen.getByTestId('chat-page')).toBeInTheDocument()
    })
    // The capped run's eleven, the reopen, then one switch-back.
    expect(switchesTo('zzq-slot-1')).toBe(13)
    expect(warn).toHaveBeenCalledTimes(1)
  })

  it('keeps taking its session back when other sessions open seconds apart', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    onTestFinished(() => { warn.mockRestore(); vi.useRealTimers() })
    const store = storeWith('zzq-slot-1')
    renderWithProviders(<ChatPanel slotKey="zzq-slot-1" conversationOnly />, { store })
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument()
    // One more switch-back than a quick run may hold, each six seconds after the last.
    for (let n = 1; n <= 11; n++) {
      vi.setSystemTime(new Date(Date.UTC(2026, 10, 9, 12, 0, 6 * n)))
      act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'zzq-other' }) })
      await waitFor(() => {
        expect(store.getState().chat.activeSlot).toBe('zzq-slot-1')
        expect(screen.getByTestId('chat-page')).toBeInTheDocument()
      })
    }
    expect(switchesTo('zzq-slot-1')).toBe(12)
    expect(warn).not.toHaveBeenCalled()
  })
})
