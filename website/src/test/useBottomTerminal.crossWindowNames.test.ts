import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Each imported store owns a different module state and event listener set.
// Storage writes are shared immediately; events queue only for OTHER windows.
vi.mock('react', () => ({ useSyncExternalStore: (_subscribe: unknown, snapshot: () => unknown) => snapshot() }))

type Store = typeof import('../hooks/useBottomTerminal')
type Frame = { store: Store; listeners: EventListener[] }
const LAYOUT = 'mc-bottom-terminal'
const NAME = 'mc-terminal-name:'
let frames: Frame[]
let current: Frame | undefined
let queue: (() => void)[]

function run<T>(frame: Frame, action: (store: Store) => T): T {
  const previous = current
  current = frame
  try { return action(frame.store) } finally { current = previous }
}
function flush() {
  // Bound the delivery loop so an accidental write-on-adoption fails clearly.
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
function tabs(frame: Frame) { return run(frame, store => store.useBottomTerminal().tabs) }

beforeEach(() => {
  localStorage.clear()
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
  localStorage.setItem(LAYOUT, JSON.stringify({ open: true, tabs: [{ id: 'a', cwd: '/one' }], activeId: 'a' }))
})
afterEach(() => { vi.restoreAllMocks(); localStorage.clear() })

describe('independent terminal window rename persistence', () => {
  it.each(['rename first', 'add first'])('retains the added tab AND custom label: %s', async order => {
    const main = await boot()
    const popout = await boot()
    let added: string | null = null
    const rename = () => run(popout, store => store.renameTab('a', 'Build logs'))
    const add = () => { added = run(main, store => store.addTab('/two')) }
    if (order === 'rename first') { rename(); add() } else { add(); rename() }
    expect(added).not.toBeNull()
    // Neither document has received the other's event yet.
    expect(tabs(popout)).toHaveLength(1)
    expect(tabs(main)).toHaveLength(2)
    flush()
    const expected = [{ id: 'a', cwd: '/one', name: 'Build logs' }, { id: added, cwd: '/two', name: undefined }]
    expect(tabs(main)).toEqual(expected)
    expect(tabs(popout)).toEqual(expected)
    expect(tabs(await boot())).toEqual(expected)
  })
})

describe('terminal label lifecycle', () => {
  it('renames different tabs concurrently without a shared names-map overwrite', async () => {
    localStorage.setItem(LAYOUT, JSON.stringify({ tabs: [{ id: 'a' }, { id: 'b' }] }))
    const main = await boot()
    const popout = await boot()
    run(main, store => store.renameTab('a', 'One'))
    run(popout, store => store.renameTab('b', 'Two'))
    flush()
    expect(tabs(main).map(t => t.name)).toEqual(['One', 'Two'])
    expect(tabs(popout)).toEqual(tabs(main))
  })

  it('last rename of the same tab wins even when its local name already matches', async () => {
    localStorage.setItem(LAYOUT, JSON.stringify({ tabs: [{ id: 'a', name: 'Original' }] }))
    const main = await boot()
    const popout = await boot()
    run(main, store => store.renameTab('a', 'Other'))
    run(popout, store => store.renameTab('a', 'Original'))
    flush()
    expect(tabs(main)[0].name).toBe('Original')
    expect(tabs(popout)[0].name).toBe('Original')
  })

  it.each(['rename first', 'layout first'])('reset overrides a stale embedded name and reload: %s', async order => {
    localStorage.setItem(LAYOUT, JSON.stringify({ tabs: [{ id: 'a', name: 'Embedded' }] }))
    const main = await boot()
    const popout = await boot()
    const reset = () => run(popout, store => store.renameTab('a', '   '))
    const layout = () => run(main, store => store.setBottomTerminalHeight(450))
    if (order === 'rename first') { reset(); layout() } else { layout(); reset() }
    expect(localStorage.getItem(NAME + 'a')).toBe('')
    flush()
    for (const frame of [main, popout, await boot()]) expect(tabs(frame)[0].name).toBeUndefined()
  })

  it.each(['rename first', 'remove first'])('removal wins and removes the label coordinate: %s', async order => {
    const main = await boot()
    const popout = await boot()
    const rename = () => run(popout, store => store.renameTab('a', 'Closing'))
    const remove = () => run(main, store => store.removeTab('a'))
    if (order === 'rename first') { rename(); remove() } else { remove(); rename() }
    flush()
    expect(tabs(main)).toEqual([])
    expect(tabs(popout)).toEqual([])
    expect(localStorage.getItem(NAME + 'a')).toBeNull()
    expect(tabs(await boot())).toEqual([])
    run(main, store => store.adoptTab('a'))
    flush()
    expect(tabs(main)[0].name).toBeUndefined()
  })

  it('cleans a label written after a concurrent removal between read and write', async () => {
    const main = await boot()
    const popout = await boot()
    const set = vi.mocked(Storage.prototype.setItem).getMockImplementation()!
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === NAME + 'a') run(main, store => store.removeTab('a'))
      set.call(this, key, value)
    })
    run(popout, store => store.renameTab('a', 'Closing'))
    flush()
    expect(tabs(main)).toEqual([])
    expect(tabs(popout)).toEqual([])
    expect(localStorage.getItem(NAME + 'a')).toBeNull()
  })

  it('reclaims crash residue on reload without dropping a live label or another namespace', async () => {
    localStorage.setItem(NAME + 'a', 'Kept')
    localStorage.setItem(NAME + 'orphan', 'Gone')
    localStorage.setItem('other:a', 'Unrelated')
    const main = await boot()
    expect(tabs(main)[0].name).toBe('Kept')
    expect(localStorage.getItem(NAME + 'orphan')).toBeNull()
    expect(localStorage.getItem('other:a')).toBe('Unrelated')
  })

  it('cleans labels only after the second hydrate probe drops their tabs', async () => {
    localStorage.setItem(NAME + 'a', 'Old shell')
    const main = await boot()
    const empty = { enabled: true, sessions: [] }
    run(main, store => store.reconcileRestoredTabs(empty))
    expect(localStorage.getItem(NAME + 'a')).toBe('Old shell')
    run(main, store => store.confirmRestoredTabs(empty))
    expect(localStorage.getItem(NAME + 'a')).toBeNull()
    expect(tabs(await boot())).toEqual([])
  })

  it.each(['{', 'null', '[]', '{"tabs":[null]}', '{"tabs":false}', '{"tabs":null}'])('does not garbage-collect from malformed layout %s', async raw => {
    localStorage.setItem(NAME + 'a', 'Keep until membership is known')
    localStorage.setItem(LAYOUT, raw)
    await boot()
    expect(localStorage.getItem(NAME + 'a')).toBe('Keep until membership is known')
  })

  it('normalizes per-id labels and reads the pre-tab splits shape', async () => {
    localStorage.setItem(LAYOUT, JSON.stringify({ splits: [{ id: 'a', name: 'Fallback', cwd: '/same' }] }))
    localStorage.setItem(NAME + 'a', '  ' + '😀'.repeat(121) + '  ')
    expect(tabs(await boot())).toEqual([{ id: 'a', name: '😀'.repeat(120), cwd: '/same' }])
  })

  it('ignores sessionStorage events and adopts a localStorage clear', async () => {
    const main = await boot()
    run(main, store => store.renameTab('a', 'Kept'))
    const before = tabs(main)
    run(main, () => main.listeners.forEach(cb => cb(new StorageEvent('storage', { key: NAME + 'a', storageArea: sessionStorage }))))
    expect(tabs(main)).toBe(before)
    localStorage.clear()
    run(main, () => main.listeners.forEach(cb => cb(new StorageEvent('storage', { key: null, storageArea: localStorage }))))
    expect(tabs(main)).toEqual([])
  })

  it('backs up fresh labels and resets through uiPrefs without rewriting the shared layout', async () => {
    const main = await boot()
    const prefs = await import('../lib/uiPrefs')
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 200 }))
    const layout = localStorage.getItem(LAYOUT)
    run(main, store => store.renameTab('a', 'Backed up'))
    await prefs.flushUiPrefs()
    let patch = JSON.parse(fetch.mock.calls.at(-1)![1]!.body as string).prefs
    expect(JSON.parse(patch[LAYOUT]).tabs[0].name).toBe('Backed up')
    expect(localStorage.getItem(LAYOUT)).toBe(layout)
    run(main, store => store.renameTab('a', ''))
    await prefs.flushUiPrefs()
    patch = JSON.parse(fetch.mock.calls.at(-1)![1]!.body as string).prefs
    expect(JSON.parse(patch[LAYOUT]).tabs[0].name).toBeUndefined()
    expect(localStorage.getItem(LAYOUT)).toBe(layout)
    prefs.__resetUiPrefsSyncForTests()
  })
})

describe('terminal preference recovery', () => {
  it('cold-restores a self-contained backup and does not resend it after a reload', async () => {
    const main = await boot()
    run(main, store => store.renameTab('a', 'Recovered'))
    const prefs = await import('../lib/uiPrefs')
    const backup = main.store.bottomTerminalPrefsSnapshot(localStorage.getItem(LAYOUT)!)
    localStorage.clear()
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ prefs: { [LAYOUT]: backup } })))
    expect(await prefs.hydrateUiPrefs()).toBe(1)
    expect(tabs(await boot())[0].name).toBe('Recovered')
    const reloadedPrefs = await import('../lib/uiPrefs')
    fetch.mockClear()
    await reloadedPrefs.flushUiPrefs()
    expect(fetch).not.toHaveBeenCalled()
    prefs.__resetUiPrefsSyncForTests()
    reloadedPrefs.__resetUiPrefsSyncForTests()
  })

  it('keeps an in-window edit usable when persistence is denied, without touching layout', async () => {
    const main = await boot()
    const before = localStorage.getItem(LAYOUT)
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('denied', 'SecurityError') })
    expect(() => run(main, store => store.renameTab('a', 'Local only'))).not.toThrow()
    expect(tabs(main)[0].name).toBe('Local only')
    expect(localStorage.getItem(LAYOUT)).toBe(before)
  })

  it('does not delete labels when a failed layout write leaves the tab persisted', async () => {
    const main = await boot()
    run(main, store => store.renameTab('a', 'Still persisted'))
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('denied', 'SecurityError') })
    run(main, store => store.removeTab('a'))
    expect(localStorage.getItem(NAME + 'a')).toBe('Still persisted')
    expect(tabs(await boot())[0].name).toBe('Still persisted')
  })

  it('does not collect labels when reads are denied', async () => {
    localStorage.setItem(NAME + 'a', 'Kept')
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('denied', 'SecurityError') })
    const main = await boot()
    expect(tabs(main)).toEqual([])
    get.mockRestore()
    expect(localStorage.getItem(NAME + 'a')).toBe('Kept')
  })
})

it('keeps a rename that lands after a layout writer computed its snapshot but before its write', async () => {
  const main = await boot()
  const popout = await boot()
  const set = vi.mocked(Storage.prototype.setItem).getMockImplementation()!
  let interleaved = false
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
    if (key === LAYOUT && !interleaved) {
      interleaved = true
      run(popout, store => store.renameTab('a', 'Between read and write'))
    }
    set.call(this, key, value)
  })
  const added = run(main, store => store.addTab('/two'))
  expect(interleaved).toBe(true)
  // The serialized layout really is stale; only the independent coordinate
  // preserves the rename when both documents receive their queued events.
  expect(JSON.parse(localStorage.getItem(LAYOUT)!).tabs[0].name).toBeUndefined()
  flush()
  for (const frame of [main, popout, await boot()]) {
    expect(tabs(frame).map(t => t.id)).toEqual(['a', added])
    expect(tabs(frame)[0].name).toBe('Between read and write')
  }
})

describe('volatile terminal names after a dropped write', () => {
  async function failedEdit(value = 'New', existing = 'Old') {
    if (existing) localStorage.setItem(NAME + 'a', existing)
    const main = await boot()
    const popout = await boot()
    const set = vi.mocked(Storage.prototype.setItem).getMockImplementation()!
    vi.mocked(Storage.prototype.setItem).mockImplementation(function (this: Storage, key, next) {
      if (current === main && key === NAME + 'a') throw new DOMException('denied', 'SecurityError')
      set.call(this, key, next)
    })
    run(main, store => store.renameTab('a', value))
    return { main, popout, allowWrites: () => vi.mocked(Storage.prototype.setItem).mockImplementation(set) }
  }

  it.each(['New', ''])('keeps the failed local edit %j across layout mutations and reloads only the persisted name', async value => {
    const { main } = await failedEdit(value)
    run(main, store => store.setBottomTerminalHeight(450))
    run(main, store => store.setBottomTerminalWidth(500))
    expect(tabs(main)[0].name).toBe(value || undefined)
    expect(localStorage.getItem(NAME + 'a')).toBe('Old')
    expect(tabs(await boot())[0].name).toBe('Old')
  })

  it('does not persist a failed first label through a later layout write or uiPrefs projection', async () => {
    const { main } = await failedEdit('New', '')
    run(main, store => store.setBottomTerminalHeight(450))
    expect(tabs(main)[0].name).toBe('New')
    expect(JSON.parse(main.store.bottomTerminalPrefsSnapshot(localStorage.getItem(LAYOUT)!)).tabs[0].name).toBeUndefined()
    expect(tabs(await boot())[0].name).toBeUndefined()
  })

  it('keeps the local edit when another window changes only the layout', async () => {
    const { main, popout } = await failedEdit()
    run(popout, store => store.setBottomTerminalHeight(450))
    flush()
    expect(tabs(main)[0].name).toBe('New')
    expect(tabs(popout)[0].name).toBe('Old')
  })

  it('lets an actual other-window label event supersede the volatile edit', async () => {
    const { main, popout } = await failedEdit()
    run(popout, store => store.renameTab('a', 'Other window'))
    expect(tabs(main)[0].name).toBe('New')
    flush()
    run(main, store => store.setBottomTerminalHeight(450))
    expect(tabs(main)[0].name).toBe('Other window')
  })

  it('clears the volatile edit after a successful same-window rename', async () => {
    const { main, allowWrites } = await failedEdit()
    allowWrites()
    run(main, store => store.renameTab('a', 'Saved'))
    run(main, store => store.setBottomTerminalHeight(450))
    expect(tabs(main)[0].name).toBe('Saved')
    expect(tabs(await boot())[0].name).toBe('Saved')
  })

  it.each(['local removal', 'other-window removal', 'test reset'])('forgets the volatile edit after %s, including re-adoption of the id', async how => {
    const { main, popout } = await failedEdit()
    if (how === 'test reset') run(main, store => store.__resetBottomTerminal())
    else run(how === 'local removal' ? main : popout, store => store.removeTab('a'))
    flush()
    expect(tabs(main)).toEqual([])
    run(main, store => store.adoptTab('a'))
    expect(tabs(main)[0].name).toBeUndefined()
  })
})
