/**
 * The OS toast is a surface for a user who is NOT looking at the app, and one
 * event earns one toast. Two defects, pinned here:
 *
 *  1. `useNativeNotification` had no visibility gate: it fired on every unacked
 *     arrival, so a note the in-app banner and the bell badge already showed
 *     ALSO raised an OS banner over the focused window.
 *  2. The socket layer's `approval` case constructed its own toast (tag
 *     `kirocrew-approval`) and then dispatched `addNotification`, whose count
 *     increase made `useNativeNotification` construct a second one (tag
 *     `approval_id`). Different tags, so the OS collapsed nothing: two banners
 *     per approval whenever the window was hidden.
 *
 * "Away" is the shared predicate in `hooks/windowAway.ts`: `document.hidden`
 * OR `!document.hasFocus()`. happy-dom's defaults are the focused state
 * (hidden=false, hasFocus()=true), so every "fires" case below sets one axis
 * explicitly and the "silent" case relies on none of them.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { useNativeNotification } from '../hooks/useNativeNotification'
import { isWindowAway } from '../hooks/windowAway'
import { addNotification } from '../store/notificationsSlice'
import type { Notification as AppNotification } from '../types'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()
  constructor() { WS_INSTANCES.push(this) }
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: object) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
}

/** Records every `new Notification()` — title and options — in order. */
const CONSTRUCTED: Array<{ title: string; options?: NotificationOptions }> = []
class RecordingNotification {
  static permission = 'granted'
  static requestPermission = vi.fn()
  constructor(title: string, options?: NotificationOptions) { CONSTRUCTED.push({ title, options }) }
}

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
}

function feedNote(overrides: Partial<AppNotification> = {}): AppNotification {
  return {
    kind: 'cron',
    title: 'Job done',
    body: 'Nightly sync',
    ts: '1.0',
    job_id: 'job-1',
    ...overrides,
  } as AppNotification
}

describe('isWindowAway', () => {
  afterEach(() => {
    delete (document as { hidden?: boolean }).hidden
    vi.restoreAllMocks()
  })

  it('is false only when the document is visible AND focused', () => {
    setHidden(false)
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    expect(isWindowAway()).toBe(false)
  })

  it('is true for a hidden document', () => {
    setHidden(true)
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    expect(isWindowAway()).toBe(true)
  })

  it('is true for a visible document that another window has focus over', () => {
    setHidden(false)
    vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    expect(isWindowAway()).toBe(true)
  })
})

describe('useNativeNotification only toasts while the user is away', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    CONSTRUCTED.length = 0
    vi.stubGlobal('Notification', RecordingNotification)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    delete (document as { hidden?: boolean }).hidden
  })

  function mount() {
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store }, children)
    }
    renderHook(() => useNativeNotification('Kiro Crew', '/avatar.png'), { wrapper })
    return store
  }

  it('constructs nothing for a note arriving in a visible, focused window', () => {
    setHidden(false)
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    const store = mount()

    act(() => { store.dispatch(addNotification(feedNote())) })

    expect(CONSTRUCTED).toHaveLength(0)
  })

  it('constructs one toast for a note arriving in a hidden window', () => {
    setHidden(true)
    const store = mount()

    act(() => { store.dispatch(addNotification(feedNote())) })

    expect(CONSTRUCTED).toHaveLength(1)
    expect(CONSTRUCTED[0].title).toBe('Job done')
    expect(CONSTRUCTED[0].options?.tag).toBe('job-1')
  })

  it('constructs one toast for a visible window that sits behind another application', () => {
    setHidden(false)
    vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    const store = mount()

    act(() => { store.dispatch(addNotification(feedNote())) })

    expect(CONSTRUCTED).toHaveLength(1)
  })

  it('does not re-announce a note that arrived while focused once the window loses focus', () => {
    // The note was on screen when it landed (banner + badge); leaving the
    // window afterwards is not a new event.
    setHidden(false)
    const focus = vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    const store = mount()

    act(() => { store.dispatch(addNotification(feedNote())) })
    expect(CONSTRUCTED).toHaveLength(0)

    focus.mockReturnValue(false)
    act(() => { store.dispatch({ type: 'noop/rerender' }) })

    expect(CONSTRUCTED).toHaveLength(0)
  })

  it('still asks for permission from a focused window when it is undecided', () => {
    // The gate is about attention, the prompt about capability; the two must
    // stay independent or a user who never leaves the window is never asked.
    setHidden(false)
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    class UndecidedNotification {
      static permission = 'default'
      static requestPermission = vi.fn()
      constructor(title: string, options?: NotificationOptions) { CONSTRUCTED.push({ title, options }) }
    }
    vi.stubGlobal('Notification', UndecidedNotification)
    const store = mount()

    act(() => { store.dispatch(addNotification(feedNote())) })

    expect(CONSTRUCTED).toHaveLength(0)
    expect(UndecidedNotification.requestPermission).toHaveBeenCalledTimes(1)
  })
})

describe('one approval frame yields exactly one OS toast', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    CONSTRUCTED.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('Notification', RecordingNotification)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    delete (document as { hidden?: boolean }).hidden
  })

  /** Both hooks, as `App.tsx` mounts them: the socket layer produces the
   *  approval, the feed watcher is the one constructor. */
  function mountApp() {
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    renderHook(() => {
      useWebSocket()
      useNativeNotification('Kiro Crew', '/avatar.png')
    }, { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { store, ws }
  }

  it('hidden window: one toast, tagged with the approval id, silent', () => {
    setHidden(true)
    const { store, ws } = mountApp()

    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-once-1', source: 'cron', tool: 'Bash', tool_input: '{}', ts: 1.0 },
      })
    })

    // The feed entry still lands (the toast is derived from it).
    expect(store.getState().notifications.items.find(n => n.approval_id === 'ap-once-1')).toBeDefined()
    expect(CONSTRUCTED).toHaveLength(1)
    expect(CONSTRUCTED[0].options?.tag).toBe('ap-once-1')
    expect(CONSTRUCTED[0].options?.silent).toBe(true)
    // The feed note's own title, not a generic "Approval Required".
    expect(CONSTRUCTED[0].title).toContain('Bash')
  })

  it('focused window: no toast at all; the feed entry and in-app surfaces carry it', () => {
    setHidden(false)
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    const { store, ws } = mountApp()

    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-once-2', source: 'cron', tool: 'Bash', tool_input: '{}', ts: 2.0 },
      })
    })

    expect(store.getState().notifications.items.find(n => n.approval_id === 'ap-once-2')).toBeDefined()
    expect(CONSTRUCTED).toHaveLength(0)
  })

  it('the socket layer alone constructs no toast for an approval', () => {
    // Without the feed watcher mounted nothing may fire: a second constructor
    // in useWebSocket is exactly the regression.
    setHidden(true)
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-once-3', source: 'cron', tool: 'Bash', tool_input: '{}', ts: 3.0 },
      })
    })

    expect(CONSTRUCTED).toHaveLength(0)
  })
})
