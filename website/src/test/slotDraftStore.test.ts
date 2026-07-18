import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { createSlotDraftStore } from '../utils/slotDraftStore'

const isString = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)
const isStringArray = (v: unknown): string[] | null => {
  if (!Array.isArray(v)) return null
  const arr = v.filter((x): x is string => typeof x === 'string')
  return arr.length ? arr.slice() : null
}

const TTL = 30 * 24 * 60 * 60 * 1000

describe('slotDraftStore', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => { vi.useRealTimers() })

  describe('storage backend', () => {
    it('local store uses localStorage and survives "tab close"', () => {
      const s = createSlotDraftStore<string>({ key: 'k-local', storage: 'local', sanitize: isString })
      s.save({ 'chat-1': 'hi' })
      expect(localStorage.getItem('k-local')).toBeTruthy()
      expect(sessionStorage.getItem('k-local')).toBeNull()
      expect(s.load()).toEqual({ 'chat-1': 'hi' })
    })

    it('session store uses sessionStorage, never localStorage', () => {
      const s = createSlotDraftStore<string[]>({ key: 'k-sess', storage: 'session', sanitize: isStringArray })
      s.save({ 'chat-1': ['/a'] })
      expect(sessionStorage.getItem('k-sess')).toBeTruthy()
      expect(localStorage.getItem('k-sess')).toBeNull()
    })
  })

  describe('corruption + emptiness guards', () => {
    it('returns {} on missing, corrupt, or non-object storage', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', sanitize: isString })
      expect(s.load()).toEqual({})
      localStorage.setItem('k', 'not json'); expect(s.load()).toEqual({})
      localStorage.setItem('k', '[]'); expect(s.load()).toEqual({})
      localStorage.setItem('k', 'null'); expect(s.load()).toEqual({})
    })

    it('drops slots sanitize rejects, keeps the rest', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', sanitize: isString })
      localStorage.setItem('k', JSON.stringify({ good: 'ok', num: 42, empty: '', nul: null, arr: [1] }))
      expect(s.load()).toEqual({ good: 'ok' })
    })

    it('set stores accepted, deletes rejected (empty) values', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', sanitize: isString })
      const d: Record<string, string> = { 'chat-1': 'old' }
      s.set(d, 'chat-1', 'new'); expect(d).toEqual({ 'chat-1': 'new' })
      s.set(d, 'chat-1', ''); expect(d).toEqual({})
    })

    it('set stores a defensive copy so caller mutations do not leak', () => {
      const s = createSlotDraftStore<string[]>({ key: 'k', storage: 'session', sanitize: isStringArray })
      const d: Record<string, string[]> = {}
      const live = ['/a']
      s.set(d, 'chat-1', live)
      live.push('/b')
      expect(d['chat-1']).toEqual(['/a'])
    })

    it('save swallows QuotaExceededError without throwing', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', sanitize: isString })
      const orig = Storage.prototype.setItem
      Storage.prototype.setItem = () => { throw new Error('QuotaExceeded') }
      try { expect(() => s.save({ a: 'b' })).not.toThrow() }
      finally { Storage.prototype.setItem = orig }
    })
  })

  describe('entry-count LRU', () => {
    it('evicts oldest entries when over maxEntries', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', maxEntries: 50, sanitize: isString })
      const d: Record<string, string> = {}
      for (let i = 0; i < 55; i++) d[`slot-${i}`] = `d${i}`
      s.save(d)
      expect(Object.keys(d).length).toBe(50)
      expect(d['slot-0']).toBeUndefined()
      expect(d['slot-4']).toBeUndefined()
      expect(d['slot-5']).toBe('d5')
      expect(Object.keys(s.load()).length).toBe(50)
    })

    it('set refreshes insertion order so a recently-touched slot is not evicted', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', maxEntries: 50, sanitize: isString })
      const d: Record<string, string> = {}
      for (let i = 0; i < 50; i++) d[`slot-${i}`] = `d${i}`
      s.set(d, 'slot-0', 'still typing')
      s.set(d, 'slot-new', 'brand new')
      s.save(d)
      expect(d['slot-0']).toBe('still typing')
      expect(d['slot-1']).toBeUndefined()
      expect(d['slot-new']).toBe('brand new')
    })

    it('no maxEntries = no entry cap', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', sanitize: isString })
      const d: Record<string, string> = {}
      for (let i = 0; i < 200; i++) d[`slot-${i}`] = `d${i}`
      s.save(d)
      expect(Object.keys(s.load()).length).toBe(200)
    })

    it('default (evict-before-write) caps the caller even when the persist throws', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', maxEntries: 3, sanitize: isString })
      const d: Record<string, string> = {}
      for (let i = 0; i < 6; i++) d[`slot-${i}`] = `d${i}`
      const orig = Storage.prototype.setItem
      Storage.prototype.setItem = () => { throw new Error('QuotaExceeded') }
      try { s.save(d) } finally { Storage.prototype.setItem = orig }
      // In-place eviction ran before the failed write, so the caller is capped.
      expect(Object.keys(d).length).toBe(3)
    })
  })

  describe('evictAfterWrite (evict-after-successful-persist, commentDrafts contract)', () => {
    it('syncs evictions back to the caller after a successful persist', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', maxEntries: 3, evictAfterWrite: true, sanitize: isString })
      const d: Record<string, string> = {}
      for (let i = 0; i < 6; i++) d[`slot-${i}`] = `d${i}`
      s.save(d)
      expect(Object.keys(d).sort()).toEqual(['slot-3', 'slot-4', 'slot-5'])
      expect(d['slot-0']).toBeUndefined()
      expect(d['slot-5']).toBe('d5')
      expect(Object.keys(s.load()).length).toBe(3)
    })

    it('leaves the caller whole when the persist throws (no silent data loss)', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', maxEntries: 3, evictAfterWrite: true, sanitize: isString })
      const d: Record<string, string> = {}
      for (let i = 0; i < 6; i++) d[`slot-${i}`] = `d${i}`
      const orig = Storage.prototype.setItem
      Storage.prototype.setItem = () => { throw new Error('QuotaExceeded') }
      try { s.save(d) } finally { Storage.prototype.setItem = orig }
      // Persist failed, so NO eviction is mirrored back — every draft survives.
      expect(Object.keys(d).length).toBe(6)
      expect(d['slot-0']).toBe('d0')
    })

    it('does not mutate the caller when nothing needs eviction (under cap)', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', maxEntries: 3, evictAfterWrite: true, sanitize: isString })
      const d: Record<string, string> = { 'slot-0': 'a', 'slot-1': 'b' }
      s.save(d)
      expect(d).toEqual({ 'slot-0': 'a', 'slot-1': 'b' })
      expect(s.load()).toEqual({ 'slot-0': 'a', 'slot-1': 'b' })
    })

    it('warns in DEV when paired with ttlMs (unsupported desync-prone combo)', () => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
      try {
        createSlotDraftStore<string>({ key: 'k', storage: 'local', ttlMs: TTL, evictAfterWrite: true, sanitize: isString })
        expect(warn).toHaveBeenCalledWith(expect.stringContaining('evictAfterWrite + ttlMs'))
      } finally { warn.mockRestore() }
    })
  })

  describe('byte-aware store-level LRU (Mesh-1909)', () => {
    it('evicts oldest slots until the serialized blob fits the byte budget', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', maxStoreBytes: 2000, sanitize: isString })
      const d: Record<string, string> = {}
      // 4 slots of ~800 bytes each = ~3200 > 2000 budget.
      for (let i = 0; i < 4; i++) d[`slot-${i}`] = 'x'.repeat(800)
      s.save(d)
      const persisted = s.load()
      expect(JSON.stringify(persisted).length).toBeLessThanOrEqual(2000)
      // Oldest dropped first; newest retained.
      expect(persisted['slot-0']).toBeUndefined()
      expect(persisted['slot-3']).toBe('x'.repeat(800))
    })

    it('NEVER evicts the newest slot even when it alone exceeds the budget', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', maxStoreBytes: 500, sanitize: isString })
      const d: Record<string, string> = {}
      s.set(d, 'old', 'small')
      s.set(d, 'newest', 'y'.repeat(5000)) // alone blows the 500-byte budget
      s.save(d)
      const persisted = s.load()
      // Older slot evicted, but the newest large draft survives intact — this is
      // the collapsed-vs-expanded symmetry guarantee from CR-279844240.
      expect(persisted['old']).toBeUndefined()
      expect(persisted['newest']).toBe('y'.repeat(5000))
    })

    it('evicts the MINIMAL number of oldest slots (exact survivor set)', () => {
      // 4 slots, 100-char values. Serialized blob is deterministic: each entry
      // adds 112 code units to the running total, full blob = 449. Budget 300
      // forces exactly two evictions (449 -> 337 -> 225 <= 300, stop), so the
      // two newest survive and slot-2 is never touched. Pins the accounting:
      // an off-by-one in capBytes would drop a third slot or keep a fourth.
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', maxStoreBytes: 300, sanitize: isString })
      const d: Record<string, string> = {}
      s.set(d, 'slot-0', 'a'.repeat(100))
      s.set(d, 'slot-1', 'b'.repeat(100))
      s.set(d, 'slot-2', 'c'.repeat(100))
      s.set(d, 'slot-3', 'd'.repeat(100))
      s.save(d)
      const persisted = s.load()
      expect(Object.keys(persisted).sort()).toEqual(['slot-2', 'slot-3'])
      expect(persisted['slot-2']).toBe('c'.repeat(100))
      expect(persisted['slot-3']).toBe('d'.repeat(100))
    })

    it('byte cap and entry cap compose (entry cap first, then byte cap)', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', maxEntries: 3, maxStoreBytes: 1500, sanitize: isString })
      const d: Record<string, string> = {}
      for (let i = 0; i < 6; i++) d[`slot-${i}`] = 'z'.repeat(700)
      s.save(d)
      const persisted = s.load()
      expect(Object.keys(persisted).length).toBeLessThanOrEqual(3)
      expect(JSON.stringify(persisted).length).toBeLessThanOrEqual(1500)
      expect(persisted['slot-5']).toBe('z'.repeat(700)) // newest always kept
    })
  })

  describe('TTL', () => {
    it('discards entries older than ttlMs on load', () => {
      vi.useFakeTimers(); vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', ttlMs: TTL, sanitize: isString })
      const d: Record<string, string> = {}
      s.set(d, 'chat-old', 'sensitive'); s.save(d)
      vi.setSystemTime(Date.now() + TTL + 1000)
      s.__resetForTests()
      expect(s.load()).toEqual({})
    })

    it('keeps entries touched within the TTL window', () => {
      vi.useFakeTimers(); vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', ttlMs: TTL, sanitize: isString })
      const d: Record<string, string> = {}
      s.set(d, 'chat-fresh', 'recent'); s.save(d)
      vi.setSystemTime(Date.now() + TTL - 1000)
      s.__resetForTests()
      expect(s.load()).toEqual({ 'chat-fresh': 'recent' })
    })

    it('editing a stale entry refreshes its timestamp', () => {
      vi.useFakeTimers(); vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', ttlMs: TTL, sanitize: isString })
      const d: Record<string, string> = {}
      s.set(d, 'chat-a', 'old'); s.set(d, 'chat-b', 'old'); s.save(d)
      vi.setSystemTime(Date.now() + TTL / 2)
      s.set(d, 'chat-a', 'edited'); s.save(d)
      vi.setSystemTime(Date.now() + TTL / 2 + 1000)
      s.__resetForTests()
      const reloaded = s.load()
      expect(reloaded['chat-a']).toBe('edited')
      expect(reloaded['chat-b']).toBeUndefined()
    })

    it('legacy entries without timestamps are stamped on first load (not evicted)', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', ttlMs: TTL, sanitize: isString })
      localStorage.setItem('k', JSON.stringify({ 'chat-legacy': 'pre-ttl' }))
      expect(s.load()).toEqual({ 'chat-legacy': 'pre-ttl' })
    })

    it('load persists stamped legacy timestamps so reload does not reset TTL', () => {
      vi.useFakeTimers(); vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', ttlMs: TTL, sanitize: isString })
      localStorage.setItem('k', JSON.stringify({ 'chat-legacy': 'content' }))
      s.load()
      const first = JSON.parse(localStorage.getItem('k-ts') || '{}')['chat-legacy']
      expect(first).toBeDefined()
      vi.setSystemTime(Date.now() + TTL - 1000)
      s.__resetForTests()
      s.load()
      const second = JSON.parse(localStorage.getItem('k-ts') || '{}')['chat-legacy']
      expect(second).toBe(first)
    })

    it('load persists pruned state so expired entries do not resurrect', () => {
      vi.useFakeTimers(); vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', ttlMs: TTL, sanitize: isString })
      const d: Record<string, string> = {}
      s.set(d, 'chat-expires', 'will expire'); s.save(d)
      vi.setSystemTime(Date.now() + TTL + 1000)
      s.__resetForTests()
      expect(s.load()).toEqual({})
      s.__resetForTests()
      expect(s.load()).toEqual({})
    })

    it('writes timestamps before drafts to survive partial quota failure', () => {
      const s = createSlotDraftStore<string>({ key: 'k', storage: 'local', ttlMs: TTL, sanitize: isString })
      const calls: string[] = []
      const orig = Storage.prototype.setItem
      Storage.prototype.setItem = function(k: string, v: string) {
        calls.push(k)
        if (calls.length === 2) throw new Error('QuotaExceeded')
        return orig.call(this, k, v)
      }
      try { s.save({ 'chat-1': 'content' }) }
      finally { Storage.prototype.setItem = orig }
      expect(calls[0]).toBe('k-ts')
      expect(calls[1]).toBe('k')
    })

    it('no ttlMs = no timestamp sidecar written', () => {
      const s = createSlotDraftStore<string[]>({ key: 'k', storage: 'session', sanitize: isStringArray })
      s.save({ 'chat-1': ['/a'] })
      expect(sessionStorage.getItem('k-ts')).toBeNull()
    })
  })
})
