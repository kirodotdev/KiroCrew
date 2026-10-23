/**
 * "Create a session, then open chat" actions outside the chat page itself, and
 * the history entries they leave.
 *
 * On a chat route the chat page's URL sync owns `?sid=` and writes the new
 * session's one entry; an extra `navigate('/chat')` there lands an entry Back
 * has to step through. Off the chat page each action must still open chat.
 * Covers the palette's "New session" action, its token-carrying variant, the
 * recents "+ New Session" row and the command bar's New Session command.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { renderHookWithProviders, createTestStore } from './helpers'
import { useActionsProvider } from '../components/commandPalette/providers/actionsProvider'
import { usePaletteActions } from '../components/commandPalette/paletteActions'
import { useRecentsProvider } from '../components/commandPalette/providers/recentsProvider'
import { useCommandBarOverlayRootRows } from '../apps/command-bar/CommandBarOverlayRootRows'
import type { SettingsSearchGovernance } from '../components/commandPalette/settingsSearchCore'
import { api } from '../api/client'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import type { RootState } from '../store'

globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

const NEW_SLOT = { key: 'chat-9-900', title: 'chat-9-900', messages: 0, running: false }
const CHAT_URL = '/chat/alpha?sid=chat-1-100'

function makeStore() {
  // Laid over each slice's real initial state: the create's fulfilled reducers
  // read fields a hand-written partial would leave undefined.
  const init = { type: '@@test/init' }
  return createTestStore({
    dashboard: { ...dashboardReducer(undefined, init), slots: [{ key: 'chat-1-100', title: 'Alpha', messages: 1, running: false }] } as RootState['dashboard'],
    chat: { ...chatReducer(undefined, init), activeSlot: 'chat-1-100' },
  })
}

/** Let promise chains and mutation callbacks scheduled after the create run. */
async function flush() {
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

/** The guard reads the live URL (`isOnChatRoute`), which a MemoryRouter does
 *  not move, so each case also puts the window where its router starts. */
function startAt(route: string) { window.history.replaceState(null, '', route) }

/** Render `useHook` alongside the router location, starting at `route`. */
function renderAt<T>(route: string, useHook: () => T) {
  startAt(route)
  const store = makeStore()
  const rendered = renderHookWithProviders(() => ({ value: useHook(), location: useLocation() }), { store, route })
  return { store, ...rendered }
}

/** Run `activate`, wait for the new session, and return the final location. */
async function create<T>(route: string, useHook: () => T, activate: (value: T) => unknown) {
  const r = renderAt(route, useHook)
  const before = r.result.current.location
  await act(async () => { await activate(r.result.current.value) })
  await waitFor(() => expect(r.store.getState().chat.activeSlot).toBe(NEW_SLOT.key))
  await flush()
  return { before, after: r.result.current.location }
}

type Case = { name: string; useHook: () => unknown; activate: (value: never) => unknown }

const cases: Case[] = [
  {
    name: 'palette "New session" action',
    useHook: () => useActionsProvider({ openShortcuts: () => {} }),
    activate: async (p: ReturnType<typeof useActionsProvider>) => {
      const rows = await Promise.resolve(p.search('new session'))
      rows.find(r => r.title === 'New session')!.onActivate()
    },
  },
  {
    name: 'palette new session carrying a token',
    useHook: () => usePaletteActions(),
    activate: (a: ReturnType<typeof usePaletteActions>) => a.newSessionWithToken('@file.ts'),
  },
  {
    name: 'recents "+ New Session" row',
    useHook: () => useRecentsProvider(),
    activate: async (p: ReturnType<typeof useRecentsProvider>) => {
      const rows = await Promise.resolve(p.search(''))
      rows.find(r => r.id === 'recents:new-session')!.onActivate()
    },
  },
]

describe('create-then-open-chat actions on and off the chat page', () => {
  beforeEach(() => {
    vi.spyOn(api, 'createChatSlot').mockResolvedValue(NEW_SLOT as Awaited<ReturnType<typeof api.createChatSlot>>)
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ default_memory_mode: 'persistent' } as Awaited<ReturnType<typeof api.dashboardConfig>>)
  })
  afterEach(() => { vi.restoreAllMocks(); startAt('/') })

  for (const c of cases) {
    it(`${c.name}: leaves the URL to the chat page when on it`, async () => {
      const { before, after } = await create(CHAT_URL, c.useHook, c.activate as (v: unknown) => unknown)
      // Same location object: nothing navigated, so the URL sync is the only
      // writer and the new session costs one entry.
      expect(after.key).toBe(before.key)
      expect(after.pathname + after.search).toBe(CHAT_URL)
    })

    it(`${c.name}: opens the chat page when pressed elsewhere`, async () => {
      const { after } = await create('/schedule', c.useHook, c.activate as (v: unknown) => unknown)
      expect(after.pathname).toBe('/chat')
    })
  }

  describe('command bar New Session command', () => {
    function commandBarRows(navigate: (route: string) => void) {
      return (store: ReturnType<typeof makeStore>) => () => useCommandBarOverlayRootRows({
        liveSlots: store.getState().dashboard.slots,
        unreadSlots: [],
        slotStatusDetail: {},
        simplifiedToolNames: false,
        dispatch: store.dispatch,
        navigate,
        store,
        cycleTheme: () => {},
        crewPreview: false,
        commandById: new Map(),
        apps: [],
        settingsGovernance: { decisionsEnabled: true, tipsEnabled: true, updateSwitches: { app: true, gateway: true } } as SettingsSearchGovernance,
        resolved: 'light',
        scope: null,
        query: 'new session',
        usage: {},
        attentionExpanded: false,
        setAttentionExpanded: () => {},
      })
    }

    async function runNewSession(route: string) {
      const navigate = vi.fn()
      const store = makeStore()
      startAt(route)
      const { result } = renderHookWithProviders(commandBarRows(navigate)(store), { store, route })
      const row = result.current.find(r => r.id === 'command:new-session')
      expect(row?.run).toBeTypeOf('function')
      await act(async () => { await row!.run!() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT.key))
      return navigate
    }

    it('leaves the URL to the chat page when on it', async () => {
      expect(await runNewSession(CHAT_URL)).not.toHaveBeenCalled()
    })

    it('opens the chat page when run elsewhere', async () => {
      expect(await runNewSession('/settings')).toHaveBeenCalledWith('/chat')
    })
  })
})
