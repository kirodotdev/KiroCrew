/**
 * contributes.sessionCreateItems — app rows in the Sessions New menu.
 *
 * Pinned here: the resolver (enabled apps only, the cap, the in-gateway endpoint
 * allowlist), the endpoint answer parser, the dispatch-time re-read of the app list,
 * and the invariant that a failed prepare or bind leaves no session behind.
 */
import { act, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const api = vi.hoisted(() => ({
  listApps: vi.fn(),
  prepareAppSession: vi.fn(),
  createChatSlot: vi.fn(),
  chatSlotProject: vi.fn(),
  deleteChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  setSlotColor: vi.fn(() => Promise.resolve({ ok: true })),
  dashboardConfig: vi.fn(() => Promise.resolve({ default_memory_mode: 'persistent' })),
}))
vi.mock('../api/client', () => ({ api }))
vi.mock('../lib/scrollQuiet', () => ({ whenScrollQuiet: () => Promise.resolve() }))
vi.mock('../components/AppIcon', () => ({ default: () => null }))
vi.mock('../hooks/usePreviewFlag', () => ({ useRemoteCrewChat: () => false }))

import {
  prepareAppSession,
  readPreparedSession,
  sessionCreateItems,
  SessionCreateItemLabel,
  type SessionCreateAppRecord,
  type SessionCreateItem,
} from '../apps/sessionCreateContributions'
import { useSessionCreate } from '../pages/chat-sidebar/create'
import chatReducer, { captureForkOrigin, forkMayOpen, forkOpenSettled, forkOpenStarted, setActiveSlot } from '../store/chatSlice'
import { closeCrewWindow, openCrewWindow } from '../pages/chat/crew-window/crewWindowStore'
import dashboardReducer from '../store/dashboardSlice'

const ROW = { id: 'new-workspace', label: 'New workspace session', icon: 'Package', endpoint: '/api/apps/ws/create' }

function app(over: Partial<SessionCreateAppRecord> = {}, rows: unknown = [ROW]): SessionCreateAppRecord {
  return { name: 'ws', enabled: true, manifest: { contributes: { sessionCreateItems: rows } }, ...over }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

describe('sessionCreateItems', () => {
  it('reads an enabled app and skips a disabled one', () => {
    expect(sessionCreateItems([app()])).toEqual([{ ...ROW, app: 'ws' }])
    expect(sessionCreateItems([app({ enabled: false })])).toEqual([])
  })

  it('tolerates a missing or non-array list and caps rows per app', () => {
    expect(sessionCreateItems([app({ manifest: { contributes: {} } })])).toEqual([])
    expect(sessionCreateItems([app({}, { id: 'x' })])).toEqual([])
    const many = Array.from({ length: 5 }, (_, i) => ({ ...ROW, id: `row-${i}` }))
    expect(sessionCreateItems([app({}, many)]).map(r => r.id)).toEqual(['row-0', 'row-1', 'row-2'])
    expect(sessionCreateItems([app({}, [{ ...ROW, id: 'a'.repeat(65) }])])).toEqual([])
    expect(sessionCreateItems([app({}, [{ ...ROW, endpoint: `/api/apps/ws/${'a'.repeat(500)}` }])])).toEqual([])
  })

  it.each([
    '/api/shutdown',
    '/api/apps/other/create',
    '/api/apps/ws/../other/create',
    '/api/apps/ws/%2e%2e/other/create',
    '/api/apps/ws/uninstall',
    '/api/apps/wsx/create',
    'https://example.com/api/apps/ws/create',
    '//example.com/api/apps/ws/create',
    '/apps/ws/api/create',
    '/api/apps/ws/.\\uninstall',
  ])('refuses endpoint %s', endpoint => {
    expect(sessionCreateItems([app({}, [{ ...ROW, endpoint }])])).toEqual([])
  })

  it('names the owning app beside the label', () => {
    render(<SessionCreateItemLabel item={{ ...ROW, app: 'ws' }} />)
    expect(screen.getByText('New workspace session')).toBeTruthy()
    expect(document.body.textContent).toContain('ws')
  })
})

describe('readPreparedSession', () => {
  it('takes an absolute project and an optional title', () => {
    expect(readPreparedSession({ project_dir: '/mnt/ws', title: ' T ' })).toEqual({ project: '/mnt/ws', title: 'T' })
    expect(readPreparedSession({ project_dir: 'C:\\ws' })).toEqual({ project: 'C:\\ws' })
    expect(readPreparedSession({ project_dir: '\\\\host\\share\\ws' })).toEqual({ project: '\\\\host\\share\\ws' })
  })

  it.each([
    null, 'x', {}, { project_dir: '' }, { project_dir: 'relative/dir' }, { project_dir: '/a\0b' },
    { project_dir: 3 }, { project_dir: '/mnt/ws', title: 7 },
  ])('refuses %j', raw => {
    expect(readPreparedSession(raw)).toBeNull()
  })
})

describe('prepareAppSession', () => {
  const item: SessionCreateItem = { ...ROW, app: 'ws' }

  it('calls the endpoint only when a fresh read still lists the row', async () => {
    api.listApps.mockResolvedValue([app()])
    api.prepareAppSession.mockResolvedValue({ project_dir: '/mnt/ws' })
    await expect(prepareAppSession(new QueryClient(), item)).resolves.toEqual({ project: '/mnt/ws' })
    expect(api.prepareAppSession).toHaveBeenCalledWith({ ...ROW, app: 'ws' })
  })

  it.each([
    ['disabled since render', [app({ enabled: false })]],
    ['removed since render', []],
    ['endpoint changed since render', [app({}, [{ ...ROW, endpoint: '/api/apps/ws/other' }])]],
  ])('refuses a row %s without calling the app', async (_why, apps) => {
    api.listApps.mockResolvedValue(apps)
    await expect(prepareAppSession(new QueryClient(), item)).rejects.toThrow()
    expect(api.prepareAppSession).not.toHaveBeenCalled()
  })
})

describe('useSessionCreate createAppSessionMutation', () => {
  function setup() {
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer },
      middleware: g => g({ serializableCheck: false, immutableCheck: false }),
    })
    const setNewChatError = vi.fn()
    const wrapper = ({ children }: { children: ReactNode }) => (
      <Provider store={store}><QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider></Provider>
    )
    const { result } = renderHook(() => useSessionCreate({
      setNewChatError, dispatch: store.dispatch, mode: '', onOpenSlotInNewTab: undefined,
      onSelectSlot: undefined, setRemoteCrewError: vi.fn(), setNewChatMenuOpen: vi.fn(),
    }), { wrapper })
    return { store, result, setNewChatError }
  }
  const item: SessionCreateItem = { ...ROW, app: 'ws' }

  beforeEach(() => { api.listApps.mockResolvedValue([app()]) })

  it('creates the session on the prepared project with its title', async () => {
    api.prepareAppSession.mockResolvedValue({ project_dir: '/mnt/ws', title: 'Workspace' })
    api.createChatSlot.mockResolvedValue({ key: 'NEW', title: 'Workspace', project: '/mnt/ws' })
    const { store, result } = setup()
    await act(async () => { await result.current.createAppSessionMutation.mutateAsync(item) })
    expect(api.createChatSlot.mock.calls[0][5]).toBe('Workspace')
    // The project rides the create, so the server binds it before publishing.
    expect(api.createChatSlot.mock.calls[0][14]).toBe('/mnt/ws')
    expect(api.chatSlotProject).not.toHaveBeenCalled()
    expect(store.getState().dashboard.slots.map(s => s.key)).toContain('NEW')
  })

  it('does not take the view when the person opened another session while the app prepared', async () => {
    let finish: (v: unknown) => void = () => {}
    api.prepareAppSession.mockReturnValue(new Promise(r => { finish = r }))
    api.createChatSlot.mockResolvedValue({ key: 'NEW' })
    const { store, result } = setup()
    let pending: Promise<unknown> = Promise.resolve()
    act(() => { pending = result.current.createAppSessionMutation.mutateAsync(item) })
    await waitFor(() => expect(api.prepareAppSession).toHaveBeenCalled())
    act(() => { store.dispatch(setActiveSlot('OTHER')) })
    await act(async () => { finish({ project_dir: '/mnt/ws' }); await pending })
    expect(store.getState().dashboard.slots.map(s => s.key)).toContain('NEW')
    expect(store.getState().chat.activeSlot).toBe('OTHER')
  })

  it('opens the new session once the create settles when the person stayed put', async () => {
    api.prepareAppSession.mockResolvedValue({ project_dir: '/mnt/ws' })
    api.createChatSlot.mockResolvedValue({ key: 'NEW' })
    const { store, result } = setup()
    await act(async () => { await result.current.createAppSessionMutation.mutateAsync(item) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('NEW'))
  })

  it('lets a fork clicked after the pick still open once the app session opened', async () => {
    let finish: (v: unknown) => void = () => {}
    api.prepareAppSession.mockReturnValue(new Promise(r => { finish = r }))
    api.createChatSlot.mockResolvedValue({ key: 'NEW' })
    const { store, result } = setup()
    let pending: Promise<unknown> = Promise.resolve()
    act(() => { pending = result.current.createAppSessionMutation.mutateAsync(item) })
    await waitFor(() => expect(api.prepareAppSession).toHaveBeenCalled())
    // A fork started after the pick, as its callers start one.
    act(() => { store.dispatch(forkOpenStarted()) })
    const forkOrigin = captureForkOrigin(store.getState())
    await act(async () => { finish({ project_dir: '/mnt/ws' }); await pending.catch(() => {}) })
    expect(forkMayOpen(store.getState(), forkOrigin)).toBe(true)
    act(() => { store.dispatch(forkOpenSettled()) })
  })

  it('does not take the view when a crew session opened while the create was in flight', async () => {
    api.prepareAppSession.mockResolvedValue({ project_dir: '/mnt/ws' })
    let created: (v: unknown) => void = () => {}
    api.createChatSlot.mockReturnValue(new Promise(r => { created = r }))
    const { store, result } = setup()
    let pending: Promise<unknown> = Promise.resolve()
    act(() => { pending = result.current.createAppSessionMutation.mutateAsync(item) })
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalled())
    act(() => { openCrewWindow({ instanceId: 'i-peer', key: 'peer-chat' }) })
    try {
      await act(async () => { created({ key: 'NEW' }); await pending })
      expect(store.getState().dashboard.slots.map(s => s.key)).toContain('NEW')
      expect(store.getState().chat.activeSlot).not.toBe('NEW')
    } finally {
      act(() => { closeCrewWindow() })
    }
  })

  it('does not take the view when a crew session was opened and closed again while the app prepared', async () => {
    let finish: (v: unknown) => void = () => {}
    api.prepareAppSession.mockReturnValue(new Promise(r => { finish = r }))
    api.createChatSlot.mockResolvedValue({ key: 'NEW' })
    const { store, result } = setup()
    let pending: Promise<unknown> = Promise.resolve()
    act(() => { pending = result.current.createAppSessionMutation.mutateAsync(item) })
    await waitFor(() => expect(api.prepareAppSession).toHaveBeenCalled())
    act(() => { openCrewWindow({ instanceId: 'i-peer', key: 'peer-chat' }); closeCrewWindow() })
    await act(async () => { finish({ project_dir: '/mnt/ws' }); await pending })
    expect(store.getState().dashboard.slots.map(s => s.key)).toContain('NEW')
    expect(store.getState().chat.activeSlot).not.toBe('NEW')
  })

  it('does not take the view when the person opened a crew session while the app prepared', async () => {
    let finish: (v: unknown) => void = () => {}
    api.prepareAppSession.mockReturnValue(new Promise(r => { finish = r }))
    api.createChatSlot.mockResolvedValue({ key: 'NEW' })
    const { store, result } = setup()
    let pending: Promise<unknown> = Promise.resolve()
    act(() => { pending = result.current.createAppSessionMutation.mutateAsync(item) })
    await waitFor(() => expect(api.prepareAppSession).toHaveBeenCalled())
    act(() => { openCrewWindow({ instanceId: 'i-peer', key: 'peer-chat' }) })
    try {
      await act(async () => { finish({ project_dir: '/mnt/ws' }); await pending })
      expect(store.getState().dashboard.slots.map(s => s.key)).toContain('NEW')
      expect(store.getState().chat.activeSlot).not.toBe('NEW')
    } finally {
      act(() => { closeCrewWindow() })
    }
  })

  it.each([
    ['the endpoint fails', () => api.prepareAppSession.mockRejectedValue(new Error('HTTP 500'))],
    ['the answer has no project', () => api.prepareAppSession.mockResolvedValue({ title: 'x' })],
  ])('creates no session when %s', async (_why, arrange) => {
    arrange()
    const { store, result, setNewChatError } = setup()
    await act(async () => { await result.current.createAppSessionMutation.mutateAsync(item).catch(() => {}) })
    expect(api.createChatSlot).not.toHaveBeenCalled()
    expect(store.getState().dashboard.slots).toEqual([])
    await waitFor(() => expect(setNewChatError).toHaveBeenLastCalledWith(expect.stringMatching(/\S/)))
  })

  it('publishes nothing when the server refuses the project', async () => {
    api.prepareAppSession.mockResolvedValue({ project_dir: '/mnt/gone' })
    api.createChatSlot.mockRejectedValue(new Error('Not a directory'))
    const { store, result, setNewChatError } = setup()
    await act(async () => { await result.current.createAppSessionMutation.mutateAsync(item).catch(() => {}) })
    expect(api.chatSlotProject).not.toHaveBeenCalled()
    expect(store.getState().dashboard.slots).toEqual([])
    await waitFor(() => expect(setNewChatError).toHaveBeenLastCalledWith('Not a directory'))
  })
})
