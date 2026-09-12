/** Terminal membership is authoritative in IndexedDB, not localStorage.
 * A mutex around localStorage is insufficient: another renderer can still
 * read stale cached bytes after acquiring the mutex. */
const DATABASE_NAME = 'kirocrew-terminal-state'
const STATE_STORE = 'state'
const STATE_KEY = 'terminal-state'
const ACQUIRE_TIMEOUT_MS = 5_000

export interface TerminalStateSnapshot {
  revision: number
  values: Map<string, string>
}
export interface TerminalStateCommit<T> {
  result: T
  snapshot: TerminalStateSnapshot
}

export class TerminalStateLockError extends Error {
  constructor(readonly code: 'unavailable' | 'timeout' | 'storage' | 'async-operation') {
    // Diagnostic code only; callers render a translated explanation.
    super(`terminal-state-lock:${code}`)
    this.name = 'TerminalStateLockError'
  }
}

function decode(value: unknown): TerminalStateSnapshot {
  if (value === undefined) return { revision: 0, values: new Map() }
  const record = value as { revision?: unknown; entries?: unknown }
  if (!record || !Number.isSafeInteger(record.revision) || Number(record.revision) < 0
    || !Array.isArray(record.entries)
    || record.entries.some(entry => !Array.isArray(entry) || entry.length !== 2
      || typeof entry[0] !== 'string' || typeof entry[1] !== 'string')) {
    throw new TerminalStateLockError('storage')
  }
  const values = new Map<string, string>(record.entries)
  if (values.size !== record.entries.length) throw new TerminalStateLockError('storage')
  return { revision: Number(record.revision), values }
}

function transact<T>(mode: IDBTransactionMode, operation: (values: Map<string, string>, revision: number) => T): Promise<TerminalStateCommit<T>> {
  return new Promise((resolve, reject) => {
    let db: IDBDatabase | undefined
    let transaction: IDBTransaction | undefined
    let settled = false
    let commit: TerminalStateCommit<T>
    const finish = (error?: unknown, failed = false) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      db?.close()
      if (failed) reject(error)
      else resolve(commit)
    }
    const fail = (error: unknown) => {
      finish(error, true)
      try { transaction?.abort() } catch { /* already finished */ }
    }
    const timer = setTimeout(() => fail(new TerminalStateLockError('timeout')), ACQUIRE_TIMEOUT_MS)
    try {
      if (typeof indexedDB === 'undefined') { fail(new TerminalStateLockError('unavailable')); return }
      const request = indexedDB.open(DATABASE_NAME, 1)
      request.onerror = () => fail(new TerminalStateLockError('storage'))
      request.onupgradeneeded = () => {
        if (settled) { request.transaction?.abort(); return }
        request.result.createObjectStore(STATE_STORE)
      }
      request.onsuccess = () => {
        db = request.result
        if (settled) { db.close(); return }
        db.onversionchange = () => db?.close()
        try {
          transaction = db.transaction(STATE_STORE, mode)
          transaction.onabort = () => finish(new TerminalStateLockError('storage'), true)
          transaction.onerror = () => finish(new TerminalStateLockError('storage'), true)
          transaction.oncomplete = () => finish()
          const store = transaction.objectStore(STATE_STORE)
          const acquired = store.get(STATE_KEY)
          acquired.onsuccess = () => {
            if (settled) return
            clearTimeout(timer)
            try {
              const snapshot = decode(acquired.result)
              const result = operation(snapshot.values, snapshot.revision)
              if (result != null && typeof (result as { then?: unknown }).then === 'function') {
                throw new TerminalStateLockError('async-operation')
              }
              if (mode === 'readwrite') {
                snapshot.revision++
                if (!Number.isSafeInteger(snapshot.revision)) throw new TerminalStateLockError('storage')
                store.put({ revision: snapshot.revision, entries: [...snapshot.values] }, STATE_KEY)
              }
              commit = { result, snapshot }
            } catch (error) { fail(error) }
          }
        } catch { fail(new TerminalStateLockError('storage')) }
      }
      // A blocked open is bounded by the timer. Late success closes its
      // connection without invoking an operation whose caller already gave up.
    } catch { fail(new TerminalStateLockError('unavailable')) }
  })
}

/** Mutate the canonical snapshot in one cross-window readwrite transaction.
 * operation MUST be synchronous; it must not publish UI/localStorage changes
 * or create PTYs. Publish only after this promise resolves, using the returned
 * revision to reject stale notifications. revision 0 permits one-time legacy
 * import. Capacity checks belong inside operation. Failures never retry it. */
export function withTerminalStateLock<T>(operation: (values: Map<string, string>, revision: number) => T): Promise<TerminalStateCommit<T>> {
  return transact('readwrite', operation)
}

/** Load committed state for boot or notification reconciliation, without
 * changing its revision. localStorage notifications carry no authority. */
export async function readTerminalStateSnapshot(): Promise<TerminalStateSnapshot> {
  return (await transact('readonly', () => undefined)).snapshot
}

/** Test-only: remove canonical state so a fixture can exercise first import. */
export function __resetTerminalStateForTests(): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new TerminalStateLockError('timeout')), ACQUIRE_TIMEOUT_MS)
    try {
      const request = indexedDB.deleteDatabase(DATABASE_NAME)
      request.onsuccess = () => { clearTimeout(timer); resolve() }
      request.onerror = () => { clearTimeout(timer); reject(new TerminalStateLockError('storage')) }
    } catch {
      clearTimeout(timer)
      reject(new TerminalStateLockError('unavailable'))
    }
  })
}
