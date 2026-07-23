import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { act } from 'react'
import { renderHookWithProviders, createTestStore } from './helpers'
import { useInstanceShortcuts } from '../hooks/useInstanceShortcuts'
import { IS_MAC, SHORTCUTS_ENABLED_KEY, INSTANCE_SHORTCUTS } from '../hooks/useKeyboardShortcuts'
import type { InstanceView, SsoStatus } from '../api/client'
import type { HostModel } from '../store/instancesSlice'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    ApiError,
    api: { listInstances: vi.fn(), connectInstance: vi.fn() },
  }
})
import { api } from '../api/client'
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'
// The chord is Electron-only (browsers reserve ⌘/Ctrl+digit for tab switching).
// Top-level tests run with the shell mocked as Electron; the non-Electron
// embedded path is covered via host.electron=false (relayed flag).
vi.mock('../lib/electron', () => ({ isElectron: true, isMacElectron: false }))

const conn = (over: Partial<InstanceView> = {}): InstanceView => ({
  id: 'cd-1',
  name: 'Cloud One',
  ssh_host: 'cd-1-alias',
  remote_port: 7777,
  local_port: 7778,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
  ...over,
})

const okSso: SsoStatus = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const listResp = (instances: InstanceView[]) => ({ active: true, instances, warm_set_cap: 5, sso: okSso })

const hostModel = (over: Partial<HostModel> = {}): HostModel => ({
  tabs: [{ id: 'cd-1', name: 'Cloud One', sshHost: 'cd-1-alias', state: 'connected', unread: 0 }],
  activeId: 'cd-1',
  self: null,
  macInset: false,
  electron: true,
  ...over,
})

/** Dispatch the platform-correct instance-switch chord (⌘ mac / Ctrl else). */
function pressDigit(n: number, over: KeyboardEventInit = {}) {
  const init: KeyboardEventInit = {
    code: `Digit${n}`,
    key: String(n),
    metaKey: IS_MAC,
    ctrlKey: !IS_MAC,
    bubbles: true,
    cancelable: true,
    ...over,
  }
  const ev = new KeyboardEvent('keydown', init)
  act(() => { document.dispatchEvent(ev) })
  return ev
}

/** Wait for the ['instances'] query to resolve AND re-render the hook so its
 *  keydown closure captures the loaded tab list before we dispatch a chord. */
async function loaded() {
  await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
  await act(async () => { await new Promise(r => setTimeout(r, 10)) })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(isEmbeddedPane).mockReturnValue(false)
  localStorage.clear()
})

describe('useInstanceShortcuts — top-level (Electron)', () => {
  it('digit 1 switches to Local (null pane)', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 't' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, host: null },
    })
    renderHookWithProviders(() => useInstanceShortcuts(), { store })
    await loaded()

    pressDigit(1)
    expect(store.getState().instances.activeId).toBeNull()
  })

  it('digit 2 switches to the first remote instance (no reconnect when warm+live)', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 't' } }, activeId: null, mru: ['cd-1'], unread: {}, host: null },
    })
    renderHookWithProviders(() => useInstanceShortcuts(), { store })
    await loaded()

    pressDigit(2)
    expect(store.getState().instances.activeId).toBe('cd-1')
    await new Promise(r => setTimeout(r, 0))
    expect(api.connectInstance).not.toHaveBeenCalled()
  })

  it('digit 2 reconnects a not-yet-warm instance', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn({ was_connected: true, status: { instance_id: 'cd-1', state: 'error', remote_port: 7777 } })]))
    vi.mocked(api.connectInstance).mockResolvedValue({ instance_id: 'cd-1', state: 'connected', local_port: 7778, token: 'fresh' })
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, host: null },
    })
    renderHookWithProviders(() => useInstanceShortcuts(), { store })
    await loaded()

    pressDigit(2)
    expect(store.getState().instances.activeId).toBe('cd-1')
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1'))
    await waitFor(() => expect(store.getState().instances.warm['cd-1']).toEqual({ port: 7778, token: 'fresh' }))
  })

  it('ignores a digit with no matching pane (default NOT prevented)', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 't' } }, activeId: null, mru: ['cd-1'], unread: {}, host: null },
    })
    renderHookWithProviders(() => useInstanceShortcuts(), { store })
    await loaded()

    // Only Local + 1 remote exist (indices 1 and 2). Digit 5 has no pane.
    const ev = pressDigit(5)
    expect(store.getState().instances.activeId).toBeNull()
    expect(ev.defaultPrevented).toBe(false)
  })

  it('never claims a digit beyond the advertised registry range (no drift)', async () => {
    // 8 remotes would make digit 9 a valid pane index — but the shortcuts
    // modal only advertises INSTANCE_SHORTCUTS.length chords, and the handler
    // derives its range from the same registry, so digit 9 must be ignored.
    const many = Array.from({ length: 8 }, (_, i) => conn({ id: `cd-${i + 1}`, name: `Cloud ${i + 1}`, status: { instance_id: `cd-${i + 1}`, state: 'connected', remote_port: 7777 }, was_connected: true }))
    vi.mocked(api.listInstances).mockResolvedValue(listResp(many))
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, host: null },
    })
    renderHookWithProviders(() => useInstanceShortcuts(), { store })
    await loaded()

    expect(INSTANCE_SHORTCUTS.length).toBe(6)
    const ev = pressDigit(9)
    expect(store.getState().instances.activeId).toBeNull()
    expect(ev.defaultPrevented).toBe(false)
    // ...while the last advertised digit still works.
    pressDigit(6)
    expect(store.getState().instances.activeId).toBe('cd-5')
  })

  it('does nothing when shortcuts are globally disabled', async () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 't' } }, activeId: null, mru: ['cd-1'], unread: {}, host: null },
    })
    renderHookWithProviders(() => useInstanceShortcuts(), { store })
    await loaded()

    pressDigit(2)
    expect(store.getState().instances.activeId).toBeNull()
  })

  it('ignores the chord when the wrong modifier is held', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 't' } }, activeId: null, mru: ['cd-1'], unread: {}, host: null },
    })
    renderHookWithProviders(() => useInstanceShortcuts(), { store })
    await loaded()

    // Alt+2 (chat-nav territory) must not switch instances.
    pressDigit(2, { metaKey: false, ctrlKey: false, altKey: true })
    expect(store.getState().instances.activeId).toBeNull()
  })
})

describe('useInstanceShortcuts — embedded pane (relay)', () => {
  beforeEach(() => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
  })

  it('relays the chord to the parent via mc-switch-instance (same channel as the click path)', () => {
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, host: hostModel() },
    })
    renderHookWithProviders(() => useInstanceShortcuts(), { store })
    // Embedded panes never run the instances poll.
    expect(api.listInstances).not.toHaveBeenCalled()

    const ev1 = pressDigit(1)
    expect(post).toHaveBeenCalledWith(expect.objectContaining({ type: 'mc-switch-instance', id: null }), '*')
    expect(ev1.defaultPrevented).toBe(true)

    const ev2 = pressDigit(2)
    expect(post).toHaveBeenCalledWith(expect.objectContaining({ type: 'mc-switch-instance', id: 'cd-1' }), '*')
    expect(ev2.defaultPrevented).toBe(true)
    post.mockRestore()
  })

  it('ignores a digit with no matching relayed tab', () => {
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, host: hostModel() },
    })
    renderHookWithProviders(() => useInstanceShortcuts(), { store })

    const ev = pressDigit(4) // host has only 1 tab (indices 1-2 valid)
    expect(post).not.toHaveBeenCalled()
    expect(ev.defaultPrevented).toBe(false)
    post.mockRestore()
  })

  it('does not bind when the parent shell is not Electron (host.electron=false)', () => {
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, host: hostModel({ electron: false }) },
    })
    renderHookWithProviders(() => useInstanceShortcuts(), { store })

    const ev = pressDigit(1)
    expect(post).not.toHaveBeenCalled()
    expect(ev.defaultPrevented).toBe(false)
    post.mockRestore()
  })

  it('does nothing before the parent relays a host model', () => {
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, host: null },
    })
    renderHookWithProviders(() => useInstanceShortcuts(), { store })

    const ev = pressDigit(1)
    expect(post).not.toHaveBeenCalled()
    expect(ev.defaultPrevented).toBe(false)
    post.mockRestore()
  })
})
