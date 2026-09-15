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
