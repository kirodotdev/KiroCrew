import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FileContents } from '@pierre/diffs'

import { handlePairDiffRequest, type PairDiffRequest, type PairDiffResponse } from '../pierre/diffWorker'

/**
 * These tests run the REAL jsdiff computation (the worker's whole job) — no
 * mocks — so the patch text asserted here is byte-identical to what the
 * browser worker posts back. Only the `Worker` transport is stubbed in the
 * client-wrapper suite below, because jsdom has none.
 */
describe('diffWorker handlePairDiffRequest', () => {
  it('produces a unified patch with headers, hunk, and ± rows for a real change', () => {
    const res = handlePairDiffRequest({
      id: 1,
      oldName: 'a.ts',
      newName: 'a.ts',
      oldContents: 'line one\nline two\nline three\n',
      newContents: 'line one\nline 2\nline three\n',
    })
    expect(res.ok).toBe(true)
    const patch = (res as Extract<PairDiffResponse, { ok: true }>).patch
    expect(patch).toContain('--- a.ts')
    expect(patch).toContain('+++ a.ts')
    expect(patch).toMatch(/@@ -\d+,\d+ \+\d+,\d+ @@/)
    expect(patch).toContain('-line two')
    expect(patch).toContain('+line 2')
    // Unchanged context rows carry a leading space, not a marker.
    expect(patch).toContain(' line one')
  })

  it('a null-equivalent old side yields a pure-addition patch', () => {
    const res = handlePairDiffRequest({
      id: 2,
      oldName: 'new.ts',
      newName: 'new.ts',
      oldContents: '',
      newContents: 'alpha\nbeta\n',
    })
    expect(res.ok).toBe(true)
    const patch = (res as Extract<PairDiffResponse, { ok: true }>).patch
    expect(patch).toContain('+alpha')
    expect(patch).toContain('+beta')
    expect(patch).not.toMatch(/^-[^-]/m)
  })

  it('hunk context is bounded, so unchanged bulk never enters the patch', () => {
    const bulk = Array.from({ length: 2000 }, (_, i) => `const v${i} = ${i}`).join('\n')
    const res = handlePairDiffRequest({
      id: 3,
      oldName: 'big.ts',
      newName: 'big.ts',
      oldContents: `first-line\n${bulk}\n`,
      newContents: `changed-first-line\n${bulk}\n`,
    })
    expect(res.ok).toBe(true)
    const patch = (res as Extract<PairDiffResponse, { ok: true }>).patch
    // One changed line + 3 context lines + headers: the 2000-line bulk stays out.
    expect(patch.split('\n').length).toBeLessThan(12)
    expect(patch).toContain('-first-line')
    expect(patch).toContain('+changed-first-line')
  })

  it('line breaks in filenames cannot inject patch structure', () => {
    const res = handlePairDiffRequest({
      id: 4,
      oldName: 'x.ts\n+++ forged.ts\n@@ -1,1 +1,1 @@\n+evil',
      newName: 'x.ts',
      oldContents: 'a\n',
      newContents: 'b\n',
    })
    expect(res.ok).toBe(true)
    const patch = (res as Extract<PairDiffResponse, { ok: true }>).patch
    // The crafted name is flattened to one header line: no forged header or
    // hunk row appears as its own line.
    expect(patch).not.toMatch(/^\+\+\+ forged\.ts$/m)
    expect(patch).not.toMatch(/^\+evil$/m)
    expect(patch).toContain('-a')
    expect(patch).toContain('+b')
  })
})

/** Minimal Worker stub: runs the real handler synchronously on postMessage,
 *  delivering the response on a microtask like a real worker would. */
class StubWorker {
  static instances: StubWorker[] = []
  static terminated = 0
  onmessage: ((e: { data: PairDiffResponse }) => void) | null = null
  onerror: (() => void) | null = null
  constructor() {
    StubWorker.instances.push(this)
  }
  postMessage(req: PairDiffRequest) {
    queueMicrotask(() => this.onmessage?.({ data: handlePairDiffRequest(req) }))
  }
  terminate() {
    StubWorker.terminated++
  }
}

describe('diffOffThread computePairPatch', () => {
  const file = (name: string, contents: string): FileContents => ({ name, contents })

  beforeEach(() => {
    StubWorker.instances = []
    StubWorker.terminated = 0
    vi.stubGlobal('Worker', StubWorker)
    vi.resetModules()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('resolves with the worker-computed patch', async () => {
    const { computePairPatch } = await import('../pierre/diffOffThread')
    const patch = await computePairPatch(file('x.ts', 'old\n'), file('x.ts', 'new\n'))
    expect(patch).toContain('-old')
    expect(patch).toContain('+new')
  })

  it('serves an identical pair from cache without a second worker round-trip', async () => {
    const { computePairPatch } = await import('../pierre/diffOffThread')
    const oldFile = file('y.ts', 'a\n')
    const newFile = file('y.ts', 'b\n')
    const first = await computePairPatch(oldFile, newFile)
    const postSpy = vi.spyOn(StubWorker.instances[0], 'postMessage')
    const second = await computePairPatch(oldFile, newFile)
    expect(second).toBe(first)
    expect(postSpy).not.toHaveBeenCalled()
  })

  it('NUL characters at input boundaries never collide cache entries', async () => {
    const { computePairPatch } = await import('../pierre/diffOffThread')
    // A delimiter-join key would collide these two tuples; the JSON key must not.
    const first = await computePairPatch(file('n.ts', 'a\n'), file('n.ts', 'b\u0000c\n'))
    const second = await computePairPatch(file('n.ts', 'a\u0000b\n'), file('n.ts', 'c\n'))
    expect(first).toContain('+b\u0000c')
    expect(second).toContain('+c')
    expect(second).not.toBe(first)
  })

  it('abort rejects with AbortError and terminates the sole in-flight worker', async () => {
    const { computePairPatch } = await import('../pierre/diffOffThread')
    const controller = new AbortController()
    const pending = computePairPatch(file('z.ts', 'p\n'), file('z.ts', 'q\n'), controller.signal)
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(StubWorker.terminated).toBe(1)
  })

  it('an already-aborted signal rejects without ever creating a worker', async () => {
    const { computePairPatch } = await import('../pierre/diffOffThread')
    const controller = new AbortController()
    controller.abort()
    await expect(
      computePairPatch(file('w.ts', 'p\n'), file('w.ts', 'q\n'), controller.signal),
    ).rejects.toMatchObject({ name: 'AbortError' })
    expect(StubWorker.instances).toHaveLength(0)
  })

  /** The key carries both file bodies, so the char budget is what bounds the
   *  cache's footprint — the entry cap alone lets eight large-file diffs pin
   *  hundreds of MB. */
  describe('char budget', () => {
    it('evicts the oldest entries until the footprint fits the budget', async () => {
      const mod = await import('../pierre/diffOffThread')
      const { computePairPatch, CACHE_MAX_CHARS, _diffCacheChars } = mod
      // An entry costs key (both bodies) + patch, and the patch carries `big`
      // again as a context line, so each pair is ~36% of the budget: two fit,
      // the third must evict the first.
      const big = 'x'.repeat(Math.floor(CACHE_MAX_CHARS * 0.12))
      const oldA = file('a.ts', big + '\nold-a\n')
      const newA = file('a.ts', big + '\nnew-a\n')
      await computePairPatch(oldA, newA)
      await computePairPatch(file('b.ts', big + '\nold-b\n'), file('b.ts', big + '\nnew-b\n'))
      const twoEntries = _diffCacheChars()
      expect(twoEntries).toBeLessThanOrEqual(CACHE_MAX_CHARS)
      expect(twoEntries).toBeGreaterThan(CACHE_MAX_CHARS / 2)
      await computePairPatch(file('c.ts', big + '\nold-c\n'), file('c.ts', big + '\nnew-c\n'))
      expect(_diffCacheChars()).toBeLessThanOrEqual(CACHE_MAX_CHARS)
      // Pair A was the oldest and had to go: asking again recomputes.
      const postSpy = vi.spyOn(StubWorker.instances[0], 'postMessage')
      await computePairPatch(oldA, newA)
      expect(postSpy).toHaveBeenCalledTimes(1)
    })

    it('a pair larger than the whole budget is served but never cached', async () => {
      const { computePairPatch, CACHE_MAX_CHARS, _diffCacheChars } = await import('../pierre/diffOffThread')
      const huge = 'y'.repeat(CACHE_MAX_CHARS)
      const oldF = file('h.ts', huge + '\nold\n')
      const newF = file('h.ts', huge + '\nnew\n')
      const patch = await computePairPatch(oldF, newF)
      expect(patch).toContain('+new')
      expect(_diffCacheChars()).toBe(0)
      const postSpy = vi.spyOn(StubWorker.instances[0], 'postMessage')
      await computePairPatch(oldF, newF)
      expect(postSpy).toHaveBeenCalledTimes(1)
    })

    it('a cache hit refreshes recency without changing the footprint', async () => {
      const { computePairPatch, _diffCacheChars } = await import('../pierre/diffOffThread')
      const oldF = file('r.ts', 'a\n')
      const newF = file('r.ts', 'b\n')
      await computePairPatch(oldF, newF)
      const before = _diffCacheChars()
      expect(before).toBeGreaterThan(0)
      await computePairPatch(oldF, newF)
      expect(_diffCacheChars()).toBe(before)
    })

    it('keeps exact retention through repeated eviction, bypass, abort and recreation', async () => {
      const { computePairPatch, CACHE_MAX_CHARS, _diffCacheChars } = await import('../pierre/diffOffThread')
      expect(CACHE_MAX_CHARS).toBe(4_000_000)
      const post = vi.spyOn(StubWorker.prototype, 'postMessage')
      const pair = (name: string, size = 480_000) => {
        const bulk = '文'.repeat(size)
        const oldFile = file(name, bulk + '\nold\n')
        const newFile = file(name, bulk + '\nnew\n')
        const response = handlePairDiffRequest({
          id: 0, oldName: name, newName: name,
          oldContents: oldFile.contents, newContents: newFile.contents,
        })
        if (!response.ok) throw new Error(response.error)
        // Four JSON strings plus two brackets and three commas. Account from
        // the fixture and real patch, never from the cache's running counter.
        const chars = [name, name, oldFile.contents, newFile.contents]
          .reduce((sum, text) => sum + JSON.stringify(text).length, 5) + response.patch.length
        return { oldFile, newFile, patch: response.patch, chars }
      }
      type Pair = ReturnType<typeof pair>
      let retained: Pair[] = []
      const check = () => {
        expect(_diffCacheChars()).toBe(retained.reduce((sum, entry) => sum + entry.chars, 0))
        expect(_diffCacheChars()).toBeLessThanOrEqual(4_000_000)
      }
      const request = async (entry: Pair, hit: boolean, cacheable = true) => {
        const calls = post.mock.calls.length
        expect(await computePairPatch(entry.oldFile, entry.newFile)).toBe(entry.patch)
        expect(post.mock.calls.length - calls).toBe(hit ? 0 : 1)
        if (cacheable) {
          // These fixtures each cost between one third and one half of the
          // budget: exactly two fit. Keep a separate expected two-entry LRU.
          expect(entry.chars).toBeGreaterThan(4_000_000 / 3)
          expect(entry.chars).toBeLessThan(4_000_000 / 2)
          retained = [...retained.filter(item => item !== entry), entry].slice(-2)
        }
        check()
      }
      try {
        for (let cycle = 0; cycle < 3; cycle++) {
          const a = pair(`a-${cycle}.ts`)
          const b = pair(`b-${cycle}.ts`)
          const c = pair(`c-${cycle}.ts`)
          await request(a, false)
          await request(b, false)
          await request(a, true) // Refresh A: inserting C must evict B, not A.
          await request(c, false)
          await request(a, true)
          await request(c, true)
          await request(b, false) // Evicted B is recomputed; A now leaves.
          const huge = pair(`huge-${cycle}.ts`, 1_400_000)
          expect(huge.chars).toBeGreaterThan(4_000_000)
          await request(huge, false, false)
          await request(huge, false, false)
          await request(c, true) // Oversized bypass must preserve useful entries.

          const controller = new AbortController()
          const abandoned = computePairPatch(file(`abort-${cycle}`, 'old\n'), file(`abort-${cycle}`, 'new\n'), controller.signal)
          const workers = StubWorker.instances.length
          controller.abort() // Before the stub's queued response, without sleeps.
          await expect(abandoned).rejects.toMatchObject({ name: 'AbortError' })
          expect(StubWorker.terminated).toBe(cycle + 1)
          check() // A late response from the old worker cannot populate the cache.
          await request(b, true)
          expect(StubWorker.instances).toHaveLength(workers)
          await request(a, false)
          expect(StubWorker.instances).toHaveLength(workers + 1)
          await request(b, true)
        }
      } finally {
        post.mockRestore()
      }
    })

    it('two concurrent misses for one pair count its footprint once', async () => {
      const { computePairPatch, _diffCacheChars } = await import('../pierre/diffOffThread')
      const oldF = file('c.ts', 'a\n')
      const newF = file('c.ts', 'b\n')
      // Both issued before either resolves, so both miss and both store.
      const [p1, p2] = await Promise.all([computePairPatch(oldF, newF), computePairPatch(oldF, newF)])
      expect(p1).toBe(p2)
      const once = _diffCacheChars()
      expect(once).toBeGreaterThan(0)
      // A sequential re-request is a hit: the footprint is the single-entry cost.
      await computePairPatch(oldF, newF)
      expect(_diffCacheChars()).toBe(once)
      // A fresh module holding exactly one such entry reports the same cost.
      vi.resetModules()
      const fresh = await import('../pierre/diffOffThread')
      await fresh.computePairPatch(oldF, newF)
      expect(fresh._diffCacheChars()).toBe(once)
    })
  })
})

describe('diffWorker attachPairDiffHandler', () => {
  it('wires onmessage to post the handler result back', async () => {
    const { attachPairDiffHandler } = await import('../pierre/diffWorker')
    const posted: PairDiffResponse[] = []
    const ctx = { onmessage: null as ((e: MessageEvent<PairDiffRequest>) => void) | null, postMessage: (r: PairDiffResponse) => posted.push(r) }
    attachPairDiffHandler(ctx)
    ctx.onmessage!({
      data: { id: 7, oldName: 'p.ts', newName: 'p.ts', oldContents: 'a\n', newContents: 'b\n' },
    } as MessageEvent<PairDiffRequest>)
    expect(posted).toHaveLength(1)
    expect(posted[0]).toMatchObject({ id: 7, ok: true })
    expect((posted[0] as Extract<PairDiffResponse, { ok: true }>).patch).toContain('-a')
  })

  it('a computation failure posts ok:false with the error text', async () => {
    const { handlePairDiffRequest } = await import('../pierre/diffWorker')
    // Malformed contents (non-string) makes jsdiff throw; the handler must
    // convert that into an error response instead of crashing the worker.
    const res = handlePairDiffRequest({
      id: 9,
      oldName: 'x.ts',
      newName: 'x.ts',
      oldContents: null as unknown as string,
      newContents: 'b\n',
    })
    expect(res).toMatchObject({ id: 9, ok: false })
    expect((res as Extract<PairDiffResponse, { ok: false }>).error).toBeTruthy()
  })

  it('a real worker scope gets the plumbing attached at import', async () => {
    vi.resetModules()
    const posted: PairDiffResponse[] = []
    vi.stubGlobal('WorkerGlobalScope', function WorkerGlobalScope() {})
    vi.stubGlobal('self', { onmessage: null, postMessage: (r: PairDiffResponse) => posted.push(r) })
    try {
      await import('../pierre/diffWorker')
      const scope = globalThis.self as unknown as { onmessage: ((e: MessageEvent<PairDiffRequest>) => void) | null }
      expect(scope.onmessage).toBeTypeOf('function')
      scope.onmessage!({
        data: { id: 3, oldName: 'y.ts', newName: 'y.ts', oldContents: '', newContents: 'z\n' },
      } as MessageEvent<PairDiffRequest>)
      expect(posted[0]).toMatchObject({ id: 3, ok: true })
    } finally {
      vi.unstubAllGlobals()
      vi.resetModules()
    }
  })
})
