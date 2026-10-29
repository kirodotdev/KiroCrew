/**
 * The bytes of the user's uploaded notification sounds.
 *
 * Browser-side only: a file is kept in this browser's IndexedDB under a key
 * made for each write (`newSoundFileKey`), which the sound's record carries,
 * and played from a `blob:` URL. Nothing is uploaded to or stored by the
 * gateway, so the sounds do not follow the user to another browser and are not
 * part of a host backup. The name, size, length and key that make a sound
 * pickable live in the sound settings (`useNotificationSound.ts`); this module
 * holds only the bytes.
 *
 * Every operation degrades to "nothing there": IndexedDB can be missing, blocked
 * (private windows, the look-preview sandbox) or full, and a notification sound
 * that cannot be read is silence, never an error the user has to dismiss.
 */

/** Bounds an uploaded sound file must meet. */
export const CUSTOM_FILE_LIMITS = {
  maxBytes: 256 * 1024,
  maxSeconds: 5,
} as const

const DB_NAME = 'kirocrew-notification-sounds'
const STORE = 'files'

let dbPromise: Promise<IDBDatabase | null> | null = null

/** Test-only: forget the open connection so each test gets a fresh database. */
export function __resetSoundFilesForTests(): void {
  dbPromise = null
}

function openDb(): Promise<IDBDatabase | null> {
  if (dbPromise) return dbPromise
  dbPromise = new Promise<IDBDatabase | null>(resolve => {
    try {
      if (typeof indexedDB === 'undefined') { resolve(null); return }
      const req = indexedDB.open(DB_NAME, 1)
      // Once this open has answered "nothing there", a success that arrives
      // later opens a connection nobody holds, so it is closed at once.
      let answered = false
      const answer = (db: IDBDatabase | null) => { answered = true; resolve(db) }
      req.onupgradeneeded = () => { req.result.createObjectStore(STORE) }
      req.onsuccess = () => {
        const db = req.result
        if (answered) { db.close(); return }
        // A connection the browser closes (site data cleared, a newer version
        // opened elsewhere) is forgotten, so the next call opens a fresh one
        // instead of failing on the dead one until reload.
        db.onversionchange = () => { db.close(); dbPromise = null }
        db.onclose = () => { dbPromise = null }
        answer(db)
      }
      req.onerror = () => answer(null)
      req.onblocked = () => answer(null)
    } catch {
      resolve(null)
    }
  }).then(db => {
    // A failed open is not cached: storage that was blocked may be allowed later.
    if (!db) dbPromise = null
    return db
  })
  return dbPromise
}

/** Run one request in its own transaction; null on any failure. Writes resolve
 *  only once the transaction commits, so `true` means the bytes are stored. */
async function run<T>(mode: IDBTransactionMode, op: (store: IDBObjectStore) => IDBRequest<T>): Promise<T | null> {
  const db = await openDb()
  if (!db) return null
  return new Promise<T | null>(resolve => {
    try {
      const tx = db.transaction(STORE, mode)
      const req = op(tx.objectStore(STORE))
      let value: T | null = null
      req.onsuccess = () => { value = req.result }
      tx.oncomplete = () => resolve(value)
      tx.onerror = () => resolve(null)
      tx.onabort = () => resolve(null)
    } catch {
      // A connection that can no longer start a transaction is dead: drop it
      // so the next call reopens rather than failing the same way.
      dbPromise = null
      resolve(null)
    }
  })
}

/**
 * Bytes are stored under a key made for each write, never under the sound's
 * name, and the sound's record carries that key. Two adds of the same name
 * (two tabs, or a retry after a refusal) therefore write two different
 * entries, and each one's clean-up can only ever delete its own bytes.
 *
 * The key starts with its creation time, so the clean-up can leave alone bytes
 * too new to have a record yet: an add writes its bytes before its record.
 */
export function newSoundFileKey(now: number = Date.now()): string {
  const rand = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`
  return `${now.toString(36)}-${rand}`
}

/** The shape `newSoundFileKey` produces; anything else in a record is refused. */
export const SOUND_FILE_KEY_RE = /^[0-9a-z]{1,12}-[0-9a-zA-Z-]{8,64}$/

/** When a key was made, or null for a key not of our shape. */
export function soundFileKeyTime(key: string): number | null {
  if (!SOUND_FILE_KEY_RE.test(key)) return null
  const t = parseInt(key.slice(0, key.indexOf('-')), 36)
  return Number.isFinite(t) ? t : null
}

/** How long bytes without a record are left alone: far longer than an add's
 *  write-then-record takes, so an add in progress in another tab is never cut. */
export const ORPHAN_GRACE_MS = 10 * 60 * 1000

/** True when bytes under `key` are old enough that a missing record means
 *  they are really orphaned, and a record without them really lost them.
 *  A key dated further in the future than the grace window (a clock that ran
 *  ahead, a hand-edited record) is treated as old too: otherwise it would be
 *  protected until that date, possibly forever. */
export function isPastGrace(key: string, now: number = Date.now()): boolean {
  const t = soundFileKeyTime(key)
  return t === null || now - t >= ORPHAN_GRACE_MS || t - now > ORPHAN_GRACE_MS
}

export async function putSoundFile(key: string, blob: Blob): Promise<boolean> {
  return (await run('readwrite', s => s.put(blob, key))) !== null
}

export async function getSoundFile(key: string): Promise<Blob | null> {
  const value = await run<unknown>('readonly', s => s.get(key))
  return value instanceof Blob ? value : null
}

export async function deleteSoundFile(key: string): Promise<void> {
  await run('readwrite', s => s.delete(key))
}

/**
 * Delete bytes no saved sound points at, when the Custom sounds pane opens.
 * Deleting a sound only forgets its record, so Undo can bring it back with its
 * bytes; the bytes left behind are collected here.
 *
 * `referenced` is read again right before each delete, never once up front:
 * another tab may have saved a record, or undone a delete, since the pane
 * opened. Bytes within `ORPHAN_GRACE_MS` of being written are kept whatever
 * the records say, because an add writes its bytes before its record.
 *
 * Returns every key stored when the pass began, so the caller can tell which
 * records have nothing behind them, or null when storage could not be read, in
 * which case nothing about the records may be concluded.
 */
export async function pruneSoundFiles(
  referenced: () => ReadonlySet<string>, now: () => number = Date.now,
): Promise<string[] | null> {
  const keys = await run<IDBValidKey[]>('readonly', s => s.getAllKeys())
  if (!keys) return null
  const stored: string[] = []
  for (const key of keys) {
    if (typeof key !== 'string') continue
    stored.push(key)
    if (!isPastGrace(key, now()) || referenced().has(key)) continue
    await deleteSoundFile(key)
  }
  return stored
}

/** Why a picked file cannot become a sound: too big, too long, or not audio
 *  this browser can decode. */
export type FileProblem = 'file_size' | 'file_length' | 'file_decode'

/**
 * Check a picked file against `CUSTOM_FILE_LIMITS` and measure its length,
 * which also proves this browser can play it. The size is checked first, so
 * an oversized file is refused without being read. A file that passes is then
 * decoded, to prove it plays and to measure its length exactly.
 */
export async function readAudioFile(file: Blob): Promise<{ bytes: number; seconds: number } | { problem: FileProblem }> {
  if (file.size <= 0 || file.size > CUSTOM_FILE_LIMITS.maxBytes) return { problem: 'file_size' }
  const OAC = typeof window !== 'undefined'
    ? (window.OfflineAudioContext
      || (window as unknown as { webkitOfflineAudioContext?: typeof OfflineAudioContext }).webkitOfflineAudioContext)
    : undefined
  if (!OAC) return { problem: 'file_decode' }
  let seconds: number
  try {
    // An offline context decodes without opening an output device.
    const ctx = new OAC(1, 1, 44100)
    const decoded = await ctx.decodeAudioData(await file.arrayBuffer())
    seconds = decoded.duration
  } catch {
    return { problem: 'file_decode' }
  }
  if (!Number.isFinite(seconds) || seconds <= 0) return { problem: 'file_decode' }
  if (seconds > CUSTOM_FILE_LIMITS.maxSeconds) return { problem: 'file_length' }
  return { bytes: file.size, seconds }
}
