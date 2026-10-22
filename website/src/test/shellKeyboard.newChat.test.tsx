/**
 * The new-chat shortcut (Alt+Shift+N / ⌘N) and the history entries it leaves.
 *
 * On the chat page the URL sync owns `?sid=` and writes the one entry a new
 * session gets; the shortcut must not navigate there itself. Off the chat page
 * it still has to open chat.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, waitFor } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { renderHookWithProviders, createTestStore } from './helpers'
import { useShellKeyboard } from '../shell/shortcuts/shellKeyboard'
import { api } from '../api/client'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import type { RootState } from '../store'

globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

const NEW_SLOT = { key: 'chat-9-900', title: 'chat-9-900', messages: 0, running: false }

function renderShortcut(route: string) {
  // The guard reads the live URL, which a MemoryRouter does not move.
  window.history.replaceState(null, '', route)
  // Laid over each slice's real initial state: the create's fulfilled reducers
  // read fields a hand-written partial would leave undefined.
  const init = { type: '@@test/init' }
  const store = createTestStore({
    dashboard: { ...dashboardReducer(undefined, init), slots: [{ key: 'chat-1-100', title: 'Alpha', messages: 1, running: false }] } as RootState['dashboard'],
    chat: { ...chatReducer(undefined, init), activeSlot: 'chat-1-100' },
  })
  return renderHookWithProviders(() => {
    useShellKeyboard({
      toggleFocusMode: () => {}, toggleNav: () => {}, terminalEnabled: false,
      isPopout: false, isEmbed: false, terminalPoppedOut: false, activeSlotProject: undefined,
    })
    return useLocation()
  }, { store, route })
}

describe('new-chat shortcut navigation', () => {
  beforeEach(() => {
    vi.spyOn(api, 'createChatSlot').mockResolvedValue(NEW_SLOT as Awaited<ReturnType<typeof api.createChatSlot>>)
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ default_memory_mode: 'persistent' } as Awaited<ReturnType<typeof api.dashboardConfig>>)
  })
  afterEach(() => { vi.restoreAllMocks(); window.history.replaceState(null, '', '/') })

  it('leaves the URL to the chat page when pressed on it', async () => {
    const { result, store } = renderShortcut('/chat/alpha?sid=chat-1-100')
    const before = result.current
    await act(async () => { fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true }) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT.key))
    // Let the mutation's onSuccess run; it is scheduled after the thunk settles.
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    // No navigation at all: the location object is the one we started on, so
    // the URL sync is the only writer and the new session costs one entry.
    expect(result.current.key).toBe(before.key)
    expect(result.current.pathname + result.current.search).toBe('/chat/alpha?sid=chat-1-100')
  })

  it('opens the chat page when pressed elsewhere', async () => {
    const { result, store } = renderShortcut('/schedule')
    await act(async () => { fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true }) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT.key))
    await waitFor(() => expect(result.current.pathname).toBe('/chat'))
  })
})
