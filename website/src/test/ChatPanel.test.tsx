/**
 * The app SDK ChatPanel mounts ChatPage for the session the app hands it. It
 * adds a row for a session the slot list has not caught up with yet, and shows
 * the chat only once that session is open and active: a session that is gone
 * or fails to open never puts another conversation in its place (#25341).
 * ChatPage is stubbed, so a rendered chat is the active slot on screen.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { addSlotOptimistic, removeSlotOptimistic } from '../store/dashboardSlice'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'
import { i18nT } from '../i18n/t'
import { clearSlotSuccession, recordSlotSuccession } from '../utils/slotSuccession'
import ChatPanel from '../app-sdk/ChatPanel'

vi.mock('../pages/ChatPage', () => ({
  default: (props: { embedded?: boolean; embedMode?: 'chat' | 'sessions'; noUrlSync?: boolean }) => (
    <div
      data-testid="chat-page"
      data-embedded={props.embedded ? 'true' : 'false'}
      data-embed-mode={props.embedMode ?? 'unset'}
      data-no-url-sync={props.noUrlSync === undefined ? 'unset' : String(props.noUrlSync)}
    >
      ChatPage
    </div>
  ),
}))

// Mirrors the real thunk: a live session becomes the active slot; a deleted one
// rejects with the 404 payload and leaves the previous active slot in place.
// Records whether the slot list had the row when the switch ran.
const goneSlots = vi.hoisted(() => new Set<string>())
const listedAtSwitch = vi.hoisted(() => [] as boolean[])
const switchSlot = vi.hoisted(() => vi.fn((key: string) => (dispatch: (a: unknown) => void, getState: () => RootState) => {
  listedAtSwitch.push(getState().dashboard.slots.some(s => s.key === key))
  if (goneSlots.has(key)) return { unwrap: () => Promise.reject({ status: 404, message: 'not found' }) }
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
})
