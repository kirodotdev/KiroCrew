/**
 * The byte store behind uploaded notification sounds, and the check a picked
 * file must pass before it can become one.
 *
 * IndexedDB is faked with a small in-memory stand-in that keeps the async
 * request / transaction-complete shape the module relies on: a write counts
 * only once its transaction commits.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

import {
  CUSTOM_FILE_LIMITS, putSoundFile, getSoundFile, deleteSoundFile, pruneSoundFiles, readAudioFile,
  newSoundFileKey, soundFileKeyTime, isPastGrace, ORPHAN_GRACE_MS, SOUND_FILE_KEY_RE,
  __resetSoundFilesForTests,
} from '../hooks/customSoundFiles'

type Handler = (() => void) | null

/** A minimal IndexedDB: one database, one store, callbacks fired on a later
 *  tick like the real thing. `failWrites` aborts every readwrite transaction. */
function fakeIndexedDb(opts: { failOpen?: boolean; failWrites?: boolean } = {}) {
  const data = new Map<IDBValidKey, unknown>()
  const later = (fn: () => void) => setTimeout(fn, 0)
  const db = {
    createObjectStore: vi.fn(),
    transaction: (_store: string, mode: IDBTransactionMode) => {
      const tx = { oncomplete: null as Handler, onerror: null as Handler, onabort: null as Handler, objectStore: null as unknown }
      const request = (apply: () => unknown) => {
        const req = { result: undefined as unknown, onsuccess: null as Handler, onerror: null as Handler }
        later(() => {
          if (mode === 'readwrite' && opts.failWrites) { tx.onabort?.(); return }
          req.result = apply()
          req.onsuccess?.()
          tx.oncomplete?.()
        })
        return req
      }
      tx.objectStore = () => ({
        put: (v: unknown, k: IDBValidKey) => request(() => { data.set(k, v); return k }),
        get: (k: IDBValidKey) => request(() => data.get(k)),
        delete: (k: IDBValidKey) => request(() => { data.delete(k) }),
        getAllKeys: () => request(() => [...data.keys()]),
      })
      return tx as unknown as IDBTransaction & { objectStore: () => unknown }
    },
  }
  const factory = {
    open: () => {
      const req = { result: db, onsuccess: null as Handler, onerror: null as Handler, onupgradeneeded: null as Handler, onblocked: null as Handler }
      later(() => {
        if (opts.failOpen) { req.onerror?.(); return }
        req.onupgradeneeded?.()
        req.onsuccess?.()
      })
      return req
    },
  }
  return { factory, data, db }
}

const original = globalThis.indexedDB

function install(factory: unknown) {
  ;(globalThis as { indexedDB?: unknown }).indexedDB = factory
}

beforeEach(() => {
  __resetSoundFilesForTests()
})

afterEach(() => {
  install(original)
  vi.unstubAllGlobals()
})

describe('sound file store', () => {
  it('stores, reads back and deletes a file under its storage key', async () => {
    const { factory } = fakeIndexedDb()
    install(factory)
    const blob = new Blob(['abc'], { type: 'audio/wav' })
    expect(await putSoundFile('Bell', blob)).toBe(true)
    expect(await getSoundFile('Bell')).toBe(blob)
    await deleteSoundFile('Bell')
    expect(await getSoundFile('Bell')).toBeNull()
  })

  it('prune deletes only old bytes no record points at, re-reading the records before each delete', async () => {
    const { factory, data } = fakeIndexedDb()
    install(factory)
    const now = Date.UTC(2026, 9, 1)
    const old = newSoundFileKey(now - ORPHAN_GRACE_MS - 1)
    const young = newSoundFileKey(now - 1000)
    const kept = newSoundFileKey(now - ORPHAN_GRACE_MS - 5)
    const savedMeanwhile = newSoundFileKey(now - ORPHAN_GRACE_MS - 9)
    for (const k of [old, young, kept, savedMeanwhile]) await putSoundFile(k, new Blob(['x']))
    let reads = 0
    // `savedMeanwhile` gains its record (another tab's add or Undo) after the
    // pass began: a snapshot taken once up front would delete it.
    const referenced = () => (++reads > 1 ? new Set([kept, savedMeanwhile]) : new Set([kept]))
    const stored = await pruneSoundFiles(referenced, () => now)
    expect(new Set(stored)).toEqual(new Set([old, young, kept, savedMeanwhile]))
    expect(new Set(data.keys())).toEqual(new Set([young, kept, savedMeanwhile]))
  })

  it('makes a distinct, well-formed key per write and dates it', () => {
    const now = Date.UTC(2026, 9, 1)
    const a = newSoundFileKey(now)
    const b = newSoundFileKey(now)
    expect(a).not.toBe(b)
    expect(SOUND_FILE_KEY_RE.test(a)).toBe(true)
    expect(soundFileKeyTime(a)).toBe(now)
    expect(isPastGrace(a, now + ORPHAN_GRACE_MS - 1)).toBe(false)
    expect(isPastGrace(a, now + ORPHAN_GRACE_MS)).toBe(true)
    // A key not of our shape carries no date, so it is never protected.
    expect(soundFileKeyTime('Door')).toBeNull()
    expect(isPastGrace('Door', now)).toBe(true)
  })

  it('two writes for the same sound name never share bytes', async () => {
    const { factory } = fakeIndexedDb()
    install(factory)
    const theirs = new Blob(['theirs'])
    const theirKey = newSoundFileKey()
    await putSoundFile(theirKey, theirs)
    // Another add of the same name writes, is refused, and cleans up its own key.
    const myKey = newSoundFileKey()
    await putSoundFile(myKey, new Blob(['mine']))
    await deleteSoundFile(myKey)
    expect(await getSoundFile(theirKey)).toBe(theirs)
  })

  it('a connection that can no longer start a transaction is reopened on the next call', async () => {
    const first = fakeIndexedDb()
    install(first.factory)
    await putSoundFile('k', new Blob(['a']))
    // The browser closed the connection (site data cleared): transactions throw.
    first.db.transaction = () => { throw new Error('InvalidStateError') }
    expect(await getSoundFile('k')).toBeNull()
    const second = fakeIndexedDb()
    install(second.factory)
    expect(await putSoundFile('k', new Blob(['b']))).toBe(true)
    expect(second.data.has('k')).toBe(true)
  })

  it('a write that does not commit reports failure', async () => {
    const { factory, data } = fakeIndexedDb({ failWrites: true })
    install(factory)
    expect(await putSoundFile('Bell', new Blob(['a']))).toBe(false)
    expect(data.size).toBe(0)
  })

  it('missing or blocked storage reads as nothing there, never an error', async () => {
    install(undefined)
    expect(await putSoundFile('Bell', new Blob(['a']))).toBe(false)
    expect(await getSoundFile('Bell')).toBeNull()
    __resetSoundFilesForTests()
    install(fakeIndexedDb({ failOpen: true }).factory)
    expect(await getSoundFile('Bell')).toBeNull()
    await expect(pruneSoundFiles(() => new Set())).resolves.toBeNull()
  })

  it('a failed open is retried on the next call', async () => {
    install(fakeIndexedDb({ failOpen: true }).factory)
    expect(await getSoundFile('Bell')).toBeNull()
    const { factory } = fakeIndexedDb()
    install(factory)
    expect(await putSoundFile('Bell', new Blob(['a']))).toBe(true)
  })
})

describe('readAudioFile', () => {
  /** Stub the offline decoder: `duration` is what it reports, or it throws. */
  const decoder = (duration: number | 'fail') => {
    const decode = vi.fn(async () => {
      if (duration === 'fail') throw new Error('EncodingError')
      return { duration }
    })
    vi.stubGlobal('OfflineAudioContext', class { decodeAudioData = decode })
    return decode
  }
  const file = (bytes: number, type = 'audio/mpeg') => new Blob([new Uint8Array(bytes)], { type })

  it('accepts a decodable file within both limits and reports its size and length', async () => {
    decoder(1.5)
    expect(await readAudioFile(file(1000))).toEqual({ bytes: 1000, seconds: 1.5 })
  })

  it('accepts a file exactly at the limits', async () => {
    decoder(CUSTOM_FILE_LIMITS.maxSeconds)
    const r = await readAudioFile(file(CUSTOM_FILE_LIMITS.maxBytes))
    expect(r).toEqual({ bytes: CUSTOM_FILE_LIMITS.maxBytes, seconds: CUSTOM_FILE_LIMITS.maxSeconds })
  })

  it('refuses an oversized file without decoding it', async () => {
    const decode = decoder(1)
    expect(await readAudioFile(file(CUSTOM_FILE_LIMITS.maxBytes + 1))).toEqual({ problem: 'file_size' })
    expect(await readAudioFile(file(0))).toEqual({ problem: 'file_size' })
    expect(decode).not.toHaveBeenCalled()
  })

  it('refuses a sound longer than the limit', async () => {
    decoder(CUSTOM_FILE_LIMITS.maxSeconds + 0.01)
    expect(await readAudioFile(file(1000))).toEqual({ problem: 'file_length' })
  })

  it('accepts a decodable file whatever MIME type the browser reports for it', async () => {
    // Firefox without an OS-registered type reports .ogg as video/ogg, and
    // audio-only .webm is video/webm everywhere: the decoder decides, not the type.
    for (const type of ['video/ogg', 'application/ogg', 'video/webm']) {
      decoder(1.5)
      expect(await readAudioFile(file(1000, type))).toEqual({ bytes: 1000, seconds: 1.5 })
    }
  })

  it('refuses a file this browser cannot decode, or that is not audio', async () => {
    decoder('fail')
    expect(await readAudioFile(file(1000))).toEqual({ problem: 'file_decode' })
    decoder('fail')
    expect(await readAudioFile(file(1000, 'image/png'))).toEqual({ problem: 'file_decode' })
    decoder(0)
    expect(await readAudioFile(file(1000))).toEqual({ problem: 'file_decode' })
  })

  it('refuses when the browser has no decoder at all', async () => {
    vi.stubGlobal('OfflineAudioContext', undefined)
    expect(await readAudioFile(file(1000))).toEqual({ problem: 'file_decode' })
  })
})

describe('isPastGrace', () => {
  it('treats a key dated beyond the grace window in the future as past it', () => {
    const now = Date.UTC(2026, 9, 1)
    expect(isPastGrace(newSoundFileKey(now + ORPHAN_GRACE_MS - 1), now)).toBe(false)
    expect(isPastGrace(newSoundFileKey(now + ORPHAN_GRACE_MS + 1), now)).toBe(true)
    // The far future a hand-edited key can claim.
    expect(isPastGrace('zzzzzzzzzzzz-aaaaaaaa', now)).toBe(true)
  })
})
