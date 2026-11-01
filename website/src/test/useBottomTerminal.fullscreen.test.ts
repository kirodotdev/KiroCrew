import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/* The bottom terminal's `fullscreen` flag (#10176) is per window, like `open`
 * (sessionStorage), and must stay out of the shared localStorage layout so a
 * maximized terminal in one tab does not grow the panel in another. It is also
 * only ever settled DOWN across windows: a foreign layout change that empties
 * the panel (closing it) drops fullscreen, but one window maximizing never
 * maximizes another.
 *
 * Harness mirrors useBottomTerminal.openPerWindow.test.ts: each imported store
 * module is a window; localStorage writes are shared at once, `storage` events
 * queue for the OTHER windows, and jsdom's single sessionStorage is cleared to
 * simulate a fresh window. */
vi.mock('react', () => ({ useSyncExternalStore: (_subscribe: unknown, snapshot: () => unknown) => snapshot() }))

type Store = typeof import('../hooks/useBottomTerminal')
type Frame = { store: Store; listeners: EventListener[] }
const LAYOUT = 'mc-bottom-terminal'
const OPEN = 'mc-bottom-terminal-open'
const FULLSCREEN = 'mc-bottom-terminal-fullscreen'
let frames: Frame[]
let current: Frame | undefined
let queue: (() => void)[]

function run<T>(frame: Frame, action: (store: Store) => T): T {
  const previous = current
  current = frame
  try { return action(frame.store) } finally { current = previous }
}
function flush() {
  for (let n = 0; queue.length && n < 100; n++) queue.shift()!()
  expect(queue).toHaveLength(0)
}
async function boot(): Promise<Frame> {
  const frame = { listeners: [] } as unknown as Frame
  current = frame
  vi.resetModules()
  frame.store = await import('../hooks/useBottomTerminal')
  current = undefined
  frames.push(frame)
  return frame
}
const snapshot = (frame: Frame) => run(frame, store => store.useBottomTerminal())

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  frames = []
  queue = []
  current = undefined
  vi.spyOn(window, 'addEventListener').mockImplementation((type, cb) => {
    if (type === 'storage' && current) current.listeners.push(cb as EventListener)
  })
  const set = Storage.prototype.setItem
  const remove = Storage.prototype.removeItem
  const notify = (key: string, oldValue: string | null, newValue: string | null) => {
    if (oldValue === newValue) return
    for (const frame of frames) {
      if (frame === current) continue
      queue.push(() => run(frame, () => {
        const event = new StorageEvent('storage', { key, oldValue, newValue, storageArea: localStorage })
        for (const listener of frame.listeners) listener(event)
      }))
    }
  }
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
    const oldValue = this.getItem(key)
    set.call(this, key, value)
    if (this === localStorage) notify(key, oldValue, value)
  })
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key) {
    const oldValue = this.getItem(key)
    remove.call(this, key)
    if (this === localStorage) notify(key, oldValue, null)
  })
})
afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear() })

describe('bottom terminal fullscreen flag', () => {
  it('toggles on and off, persisting to sessionStorage, out of the shared layout', async () => {
    const main = await boot()
    run(main, store => store.openBottomTerminal())
    run(main, store => store.toggleTerminalFullscreen())
    expect(snapshot(main).fullscreen).toBe(true)
    expect(sessionStorage.getItem(FULLSCREEN)).toBe('1')
    // The shared layout never carries the per-window flag.
    expect(JSON.parse(localStorage.getItem(LAYOUT)!)).not.toHaveProperty('fullscreen')

    run(main, store => store.toggleTerminalFullscreen())
    expect(snapshot(main).fullscreen).toBe(false)
    expect(sessionStorage.getItem(FULLSCREEN)).toBeNull()
  })

  it('maximizing from a closed panel opens it', async () => {
    const main = await boot()
    expect(snapshot(main).open).toBe(false)
    run(main, store => store.setTerminalFullscreen(true))
    expect(snapshot(main).open).toBe(true)
    expect(snapshot(main).fullscreen).toBe(true)
    expect(snapshot(main).tabs).toHaveLength(1)
  })

  it('closing the panel clears fullscreen so a reopen is not maximized', async () => {
    const main = await boot()
    run(main, store => store.openBottomTerminal())
    run(main, store => store.toggleTerminalFullscreen())
    expect(snapshot(main).fullscreen).toBe(true)
    run(main, store => store.closeBottomTerminal())
    expect(snapshot(main).open).toBe(false)
    expect(snapshot(main).fullscreen).toBe(false)
    expect(sessionStorage.getItem(FULLSCREEN)).toBeNull()
    run(main, store => store.openBottomTerminal())
    expect(snapshot(main).fullscreen).toBe(false)
  })

  it('is per window: maximizing one leaves another unmaximized', async () => {
    const main = await boot()
    run(main, store => store.openBottomTerminal())
    const other = await boot()
    run(other, store => store.openBottomTerminal())
    flush()
    run(main, store => store.toggleTerminalFullscreen())
    flush()
    expect(snapshot(main).fullscreen).toBe(true)
    expect(snapshot(other).fullscreen).toBe(false)
  })

  it('drops its own fullscreen when another window empties the shared tab list', async () => {
    const main = await boot()
    run(main, store => store.openBottomTerminal())
    run(main, store => store.toggleTerminalFullscreen())
    const other = await boot()
    flush()
    const id = snapshot(main).tabs[0].id
    run(other, store => store.removeTab(id))
    // jsdom's single sessionStorage: `other`'s own set() removed main's flags;
    // restore them before delivering the event so the assertion holds only if
    // main's own listener drops them when the panel closes.
    sessionStorage.setItem(OPEN, '1')
    sessionStorage.setItem(FULLSCREEN, '1')
    flush()
    expect(snapshot(main).tabs).toEqual([])
    expect(snapshot(main).open).toBe(false)
    expect(snapshot(main).fullscreen).toBe(false)
    expect(sessionStorage.getItem(FULLSCREEN)).toBeNull()
  })

  it('a reload of the owning window restores fullscreen; a fresh window starts un-maximized', async () => {
    const main = await boot()
    run(main, store => store.openBottomTerminal())
    run(main, store => store.toggleTerminalFullscreen())
    // Same window reloaded: sessionStorage survives.
    const reloaded = await boot()
    expect(snapshot(reloaded).open).toBe(true)
    expect(snapshot(reloaded).fullscreen).toBe(true)
    // A new window: its own empty sessionStorage.
    sessionStorage.clear()
    const fresh = await boot()
    expect(snapshot(fresh).open).toBe(false)
    expect(snapshot(fresh).fullscreen).toBe(false)
  })

  it('settles a stale fullscreen flag against an empty shared layout at boot', async () => {
    sessionStorage.setItem(FULLSCREEN, '1')
    const main = await boot()
    expect(snapshot(main).fullscreen).toBe(false)
    expect(sessionStorage.getItem(FULLSCREEN)).toBeNull()
  })

  it('a fullscreen toggle never rewrites the shared layout, so a concurrent tab in another window survives', async () => {
    // Window A: open with one tab, maximized.
    const a = await boot()
    run(a, store => store.openBottomTerminal())
    run(a, store => store.toggleTerminalFullscreen())
    const aTab = snapshot(a).tabs[0].id
    // Window B boots from the same shared layout, then adds a tab. B's write
    // puts both tabs in the shared localStorage layout.
    const b = await boot()
    flush()
    const bTab = run(b, store => store.addTab())
    // A toggles fullscreen BEFORE it processes B's queued storage event. The
    // flag flip must NOT re-persist A's (now stale, one-tab) layout over B's.
    run(a, store => store.toggleTerminalFullscreen())
    const layout = JSON.parse(localStorage.getItem(LAYOUT)!)
    const layoutTabIds = layout.tabs.map((t: { id: string }) => t.id)
    expect(layoutTabIds).toContain(aTab)
    expect(layoutTabIds).toContain(bTab)
    // The shared layout never carries the per-window fullscreen flag.
    expect(layout).not.toHaveProperty('fullscreen')
  })
})
