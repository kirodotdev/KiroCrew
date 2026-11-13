/**
 * ⌘O / Ctrl+O pops the active session out to its own window (#28521).
 *
 *   - The registry carries `pop-out-session` on mod+O, the label of the chat
 *     header's pop-out button, and no other registry chord shares it.
 *   - The chord opens the active session's window, titled like the header
 *     button's open, and focuses that window when the session is already out.
 *   - A popout or embedded pane has no pop-out button, and with no active
 *     session there is nothing to pop out, so there the chord is not claimed
 *     and the browser keeps it.
 *   - A held chord's auto-repeat opens nothing more.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import type { ChatSlot } from '../types'

vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useNavigate: () => vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, { get: () => vi.fn().mockResolvedValue([]) }),
}))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: '' }) }))
vi.mock('../hooks/useInstanceShortcuts', () => ({ useInstanceShortcuts: () => {} }))
vi.mock('../hooks/useAutoConnectInstances', () => ({ useAutoConnectInstances: () => {} }))
vi.mock('../hooks/useCommandPalette', () => ({ useCommandPalette: () => ({ open: false, setOpen: () => {} }) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({}) }))

const popout = vi.hoisted(() => ({
  open: vi.fn(),
  focus: vi.fn(),
  out: new Set<string>(),
}))
vi.mock('../utils/chatPopout', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  openPopout: popout.open,
  focusPopout: popout.focus,
  getSnapshot: () => popout.out,
}))

import { useShellKeyboard } from '../shell/shortcuts/shellKeyboard'
import { SHORTCUT_LABEL_KEY } from '../hooks/useKeyboardShortcuts'
import { matchShortcutEvent, resolveShortcut, resolveShortcuts, shortcutEntry } from '../lib/shortcutRegistry'

function harness(opts: { isPopout?: boolean; isEmbed?: boolean; activeSlot?: string | null } = {}) {
  const defaults = createTestStore().getState()
  const slots = [{ key: 'chat-a', title: 'Fix the build', messages: 1, running: false }] as ChatSlot[]
  const store = createTestStore({
    dashboard: { ...defaults.dashboard, slots, slotsLoaded: true },
    chat: { ...defaults.chat, activeSlot: opts.activeSlot === undefined ? 'chat-a' : opts.activeSlot },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}><Provider store={store}><MemoryRouter>{children}</MemoryRouter></Provider></QueryClientProvider>
  )
  renderHook(() => useShellKeyboard({
    toggleFocusMode: () => {}, toggleNav: () => {}, terminalEnabled: false, isPopout: !!opts.isPopout,
    isEmbed: !!opts.isEmbed, terminalPoppedOut: false, activeSlotProject: undefined,
  }), { wrapper })
}

/** Ctrl+O, the non-Mac spelling of mod+O. True when the handler claimed it. */
function press(init: KeyboardEventInit = {}): boolean {
  const event = new KeyboardEvent('keydown', { code: 'KeyO', key: 'o', ctrlKey: true, cancelable: true, bubbles: true, ...init })
  return !document.dispatchEvent(event)
}

beforeEach(() => {
  localStorage.clear()
  popout.open.mockReset()
  popout.focus.mockReset()
  popout.out.clear()
})

describe('pop-out-session registry entry', () => {
  it('is mod+O on both platforms, labelled like the header button', () => {
    expect(shortcutEntry('pop-out-session')?.dispatch).toBe('registry')
    expect(resolveShortcut('pop-out-session', {}, 'mac').primary).toEqual({ key: 'o', mod: true })
    expect(resolveShortcut('pop-out-session', {}, 'other').primary).toEqual({ key: 'o', mod: true })
    expect(SHORTCUT_LABEL_KEY['pop-out-session']).toBe('pages.chatPage.pop_out_session_to_its_own_window')
  })

  it('is the only registry chord on ⌘O / Ctrl+O', () => {
    const ev = { code: 'KeyO', key: 'o', altKey: false, shiftKey: false }
    expect(matchShortcutEvent({ ...ev, metaKey: true, ctrlKey: false }, resolveShortcuts({}, 'mac'), 'mac')).toBe('pop-out-session')
    expect(matchShortcutEvent({ ...ev, metaKey: false, ctrlKey: true }, resolveShortcuts({}, 'other'), 'other')).toBe('pop-out-session')
  })
})

describe('⌘O / Ctrl+O in the main window', () => {
  it('opens the active session in its own window and claims the keystroke', () => {
    harness()
    expect(press()).toBe(true)
    expect(popout.open).toHaveBeenCalledTimes(1)
    expect(popout.open).toHaveBeenCalledWith('chat-a', 'Fix the build')
    expect(popout.focus).not.toHaveBeenCalled()
  })

  it('focuses the window when the session is already popped out', () => {
    popout.out.add('chat-a')
    harness()
    expect(press()).toBe(true)
    expect(popout.focus).toHaveBeenCalledWith('chat-a')
    expect(popout.open).not.toHaveBeenCalled()
  })

  it('ignores the auto-repeat of a held chord', () => {
    harness()
    press()
    press({ repeat: true })
    expect(popout.open).toHaveBeenCalledTimes(1)
  })

  it('is left to the browser with no active session', () => {
    harness({ activeSlot: null })
    expect(press()).toBe(false)
    expect(popout.open).not.toHaveBeenCalled()
    expect(popout.focus).not.toHaveBeenCalled()
  })

  it('is a miss with Shift or Alt held (exact modifier match)', () => {
    harness()
    expect(press({ shiftKey: true })).toBe(false)
    expect(press({ altKey: true })).toBe(false)
    expect(popout.open).not.toHaveBeenCalled()
  })
})

describe('⌘O / Ctrl+O where there is no pop-out button', () => {
  it('is left to the browser in a popout window', () => {
    harness({ isPopout: true })
    expect(press()).toBe(false)
    expect(popout.open).not.toHaveBeenCalled()
  })

  it('is left to the browser in an embedded pane', () => {
    harness({ isEmbed: true })
    expect(press()).toBe(false)
    expect(popout.open).not.toHaveBeenCalled()
  })
})
