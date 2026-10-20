import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/* The bottom terminal's `open` flag is per window (sessionStorage); the tab
 * list stays shared (localStorage). Opening the panel in one same-origin tab
 * must not open it in another: that other tab's mount is what used to attach
 * to the same PTY and take it over (#7638).
 *
 * Harness: each imported store owns a different module state and listener set,
 * standing in for a window. localStorage writes are shared at once; `storage`
 * events queue for the OTHER windows only. jsdom has one sessionStorage, so a
 * fresh window is simulated by clearing it before booting the next store. */
vi.mock('react', () => ({ useSyncExternalStore: (_subscribe: unknown, snapshot: () => unknown) => snapshot() }))

type Store = typeof import('../hooks/useBottomTerminal')
type Frame = { store: Store; listeners: EventListener[] }
const LAYOUT = 'mc-bottom-terminal'
const OPEN = 'mc-bottom-terminal-open'
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

describe('bottom terminal open flag is per window', () => {
  it('opening the panel in one window leaves the other closed, with the tab list shared', async () => {
    const main = await boot()
    const other = await boot()
    run(main, store => store.openBottomTerminal('/one'))
    flush()
    expect(snapshot(main).open).toBe(true)
    expect(snapshot(other).open).toBe(false)
    expect(snapshot(other).tabs).toEqual(snapshot(main).tabs)
    expect(snapshot(other).activeId).toBe(snapshot(main).activeId)
  })

  it('closing the panel in one window does not close it in another', async () => {
    const main = await boot()
    run(main, store => store.openBottomTerminal())
    const other = await boot()
    run(other, store => store.openBottomTerminal())
    flush()
    run(main, store => store.closeBottomTerminal())
    // The mechanism under test: a close changes nothing in the shared layout,
    // so no `storage` event reaches the other window at all. The old code
    // wrote `open: false` into the layout and the other window adopted it.
    expect(queue).toHaveLength(0)
    flush()
    expect(snapshot(main).open).toBe(false)
    expect(snapshot(other).open).toBe(true)
  })

  it('the shared layout carries no open flag', async () => {
    const main = await boot()
    run(main, store => store.openBottomTerminal())
    const layout = JSON.parse(localStorage.getItem(LAYOUT)!)
    expect(layout).not.toHaveProperty('open')
    expect(layout.tabs).toHaveLength(1)
    expect(sessionStorage.getItem(OPEN)).toBe('1')
  })

  it('a reload of the owning window restores the panel; a fresh window starts closed', async () => {
    const main = await boot()
    run(main, store => store.openBottomTerminal())
    // Same window, reloaded: sessionStorage survives.
    const reloaded = await boot()
    expect(snapshot(reloaded).open).toBe(true)
    expect(snapshot(reloaded).tabs).toHaveLength(1)
    // A new window: its own empty sessionStorage, the shared tab list.
    sessionStorage.clear()
    const fresh = await boot()
    expect(snapshot(fresh).open).toBe(false)
    expect(snapshot(fresh).tabs).toHaveLength(1)
  })

  it('ignores an open flag left in the shared layout by an older build', async () => {
    localStorage.setItem(LAYOUT, JSON.stringify({ open: true, tabs: [{ id: 'a' }], activeId: 'a' }))
    const main = await boot()
    expect(snapshot(main).open).toBe(false)
    expect(snapshot(main).tabs).toEqual([{ id: 'a' }])
  })

  it('closes when another window removes the last tab, and clears its own flag', async () => {
    const main = await boot()
    run(main, store => store.openBottomTerminal())
    const other = await boot()
    flush()
    const id = snapshot(main).tabs[0].id
    run(other, store => store.removeTab(id))
    // jsdom has one sessionStorage, so `other`'s own `set()` already removed
    // the key. Put it back before delivering the event: the assertion below
    // then holds only if `main`'s listener clears the flag itself.
    sessionStorage.setItem(OPEN, '1')
    flush()
    expect(snapshot(main).tabs).toEqual([])
    expect(snapshot(main).open).toBe(false)
    expect(sessionStorage.getItem(OPEN)).toBeNull()
  })

  it('stays open when another window changes the tab list without emptying it', async () => {
    const main = await boot()
    run(main, store => store.openBottomTerminal())
    const other = await boot()
    flush()
    run(other, store => store.addTab('/two'))
    flush()
    expect(snapshot(main).open).toBe(true)
    expect(snapshot(main).tabs).toHaveLength(2)
    // No flag write happened on the adoption path: the stored flag is intact.
    expect(sessionStorage.getItem(OPEN)).toBe('1')
  })

  it('settles a stale open flag against an empty shared layout at boot', async () => {
    sessionStorage.setItem(OPEN, '1')
    const main = await boot()
    expect(snapshot(main).open).toBe(false)
    expect(sessionStorage.getItem(OPEN)).toBeNull()
  })
})
