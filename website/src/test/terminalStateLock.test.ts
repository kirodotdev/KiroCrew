import { afterEach, describe, expect, it, vi } from 'vitest'
import { withTerminalStateLock, readTerminalStateSnapshot } from '../utils/terminalStateLock'

// Event controls for failure/publication boundaries only. Cross-window
// transaction serialization is verified against native IndexedDB in Chromium.
function databaseEvents(stored?: unknown) {
  const get = { result: stored, onsuccess: undefined as undefined | (() => void) }
  const put = vi.fn()
  const transaction = {
    oncomplete: undefined as undefined | (() => void),
    onabort: undefined as undefined | (() => void),
    objectStore: () => ({ get: () => get, put }),
    abort: vi.fn(),
  }
  const db = { close: vi.fn(), transaction: vi.fn(() => transaction) }
  const open = { result: db, onsuccess: undefined as undefined | (() => void) }
  vi.stubGlobal('indexedDB', { open: () => open })
  return { open, db, transaction, get, put }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('canonical terminal state transaction', () => {
  it('fails closed without IndexedDB even if Web Locks exists', async () => {
    vi.stubGlobal('navigator', { locks: { request: vi.fn() } })
    vi.stubGlobal('indexedDB', undefined)
    const operation = vi.fn()
    await expect(withTerminalStateLock(operation)).rejects.toMatchObject({ code: 'unavailable' })
    expect(operation).not.toHaveBeenCalled()
  })

  it('publishes result and incremented canonical snapshot only after commit', async () => {
    const events = databaseEvents({ revision: 7, entries: [['A', 'old']] })
    const published = vi.fn()
    const pending = withTerminalStateLock((values, revision) => {
      expect(revision).toBe(7)
      expect(values.get('A')).toBe('old')
      values.set('A', 'new')
      return 'result'
    }).then(published)
    events.open.onsuccess?.()
    expect(events.db.transaction).toHaveBeenCalledWith('state', 'readwrite')
    events.get.onsuccess?.()
    await Promise.resolve()
    expect(published).not.toHaveBeenCalled()
    expect(events.put).toHaveBeenCalledWith({ revision: 8, entries: [['A', 'new']] }, 'terminal-state')
    events.transaction.oncomplete?.()
    await pending
    expect(published).toHaveBeenCalledWith({ result: 'result', snapshot: { revision: 8, values: new Map([['A', 'new']]) } })
    expect(events.db.close).toHaveBeenCalledOnce()
  })

  it('returns revision zero for an absent record without writing on read', async () => {
    const events = databaseEvents()
    const pending = readTerminalStateSnapshot()
    events.open.onsuccess?.()
    events.get.onsuccess?.()
    events.transaction.oncomplete?.()
    expect(await pending).toEqual({ revision: 0, values: new Map() })
    expect(events.db.transaction).toHaveBeenCalledWith('state', 'readonly')
    expect(events.put).not.toHaveBeenCalled()
  })

  it('does not publish a mutation when its transaction aborts', async () => {
    const events = databaseEvents()
    const pending = withTerminalStateLock(values => { values.set('A', 'new'); return 1 })
    events.open.onsuccess?.()
    events.get.onsuccess?.()
    events.transaction.onabort?.()
    await expect(pending).rejects.toMatchObject({ code: 'storage' })
  })

  it('aborts without retrying when synchronous work throws, even undefined', async () => {
    const events = databaseEvents()
    const operation = vi.fn(() => { throw undefined })
    const pending = withTerminalStateLock(operation)
    events.open.onsuccess?.()
    events.get.onsuccess?.()
    await expect(pending).rejects.toBeUndefined()
    expect(operation).toHaveBeenCalledOnce()
    expect(events.transaction.abort).toHaveBeenCalledOnce()
    expect(events.put).not.toHaveBeenCalled()
  })

  it('rejects asynchronous operations before writing a snapshot', async () => {
    const events = databaseEvents()
    const pending = withTerminalStateLock(() => Promise.resolve(1))
    events.open.onsuccess?.()
    events.get.onsuccess?.()
    await expect(pending).rejects.toMatchObject({ code: 'async-operation' })
    expect(events.put).not.toHaveBeenCalled()
  })

  it('fails closed for corrupted state rather than recreating an empty registry', async () => {
    const events = databaseEvents({ revision: 3, entries: [['A', 'one'], ['A', 'two']] })
    const operation = vi.fn()
    const pending = withTerminalStateLock(operation)
    events.open.onsuccess?.()
    events.get.onsuccess?.()
    await expect(pending).rejects.toMatchObject({ code: 'storage' })
    expect(operation).not.toHaveBeenCalled()
  })

  it('bounds blocked acquisition and never invokes a late transaction callback', async () => {
    vi.useFakeTimers()
    const events = databaseEvents()
    const operation = vi.fn()
    const pending = expect(withTerminalStateLock(operation)).rejects.toMatchObject({ code: 'timeout' })
    events.open.onsuccess?.()
    await vi.advanceTimersByTimeAsync(5_000)
    await pending
    events.get.onsuccess?.()
    expect(operation).not.toHaveBeenCalled()
    expect(events.transaction.abort).toHaveBeenCalledOnce()
  })

  it('closes a database connection arriving after an open timeout', async () => {
    vi.useFakeTimers()
    const events = databaseEvents()
    const operation = vi.fn()
    const pending = expect(withTerminalStateLock(operation)).rejects.toMatchObject({ code: 'timeout' })
    await vi.advanceTimersByTimeAsync(5_000)
    await pending
    events.open.onsuccess?.()
    expect(events.db.close).toHaveBeenCalledOnce()
    expect(events.db.transaction).not.toHaveBeenCalled()
  })
})
