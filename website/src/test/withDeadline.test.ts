import { describe, it, expect, vi } from 'vitest'
import { withDeadline } from '../lib/withDeadline'
import { isDeadlineError, retryPolicy } from '../api/queryClient'
import { searchErrorCause } from '../lib/searchErrorCause'

/** A request that settles ONLY when its signal aborts — a wedged endpoint. */
const neverArrives = (signal: AbortSignal) => new Promise<string>((_res, rej) => {
  if (signal.aborted) return rej(signal.reason)
  signal.addEventListener('abort', () => rej(signal.reason), { once: true })
})

const name = (e: unknown) => (e as Error)?.name

/**
 * Executable form of this module's stated MUST: a replacement transport has to reject with a
 * `TimeoutError`-named `Error`. Asserted on a hand-built rejection, NOT one from `withDeadline`,
 * because the point is to bind whatever ships next — a pin that only exercises this module would
 * still pass while a new transport rejected with a shape none of these three consumers recognise.
 */
describe('the rejection shape a replacement transport must satisfy', () => {
  const conforming = () => Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })

  it('is recognised as a deadline, so the notice can name a timeout apart from a failure', () => {
    expect(isDeadlineError(conforming())).toBe(true)
  })

  it('classifies as timed_out, which is the cause every notice added here keys on', () => {
    expect(searchErrorCause(conforming())).toBe('timed_out')
  })

  it('is not retried, so the bound is not silently doubled by a second attempt', () => {
    expect(retryPolicy(0, conforming())).toBe(false)
  })

  describe('a shape that drifts off the contract', () => {
    // Both are plausible rejections for a transport-level bound to reach for.
    const abortShaped = () => new DOMException('aborted', 'AbortError')
    const plainError = () => new Error('deadline exceeded')

    it.each([
      ['an AbortError DOMException', abortShaped],
      ['an Error carrying no TimeoutError name', plainError],
    ])('degrades %s to the generic failed copy and retries it', (_label, make) => {
      expect(isDeadlineError(make())).toBe(false)
      expect(searchErrorCause(make())).toBe('failed')
      expect(retryPolicy(0, make())).toBe(true)
    })
  })
})

describe('withDeadline', () => {
  it('rejects with TimeoutError when the attempt never settles on its own', async () => {
    // A pending promise becomes a settled rejection, which is what a
    // "has this settled?" gate needs to move on.
    await expect(withDeadline(20, undefined, neverArrives)).rejects.toSatisfy(
      e => name(e) === 'TimeoutError')
  })

  it('resolves untouched when the attempt beats the deadline', async () => {
    await expect(withDeadline(5_000, undefined, () => Promise.resolve('ok'))).resolves.toBe('ok')
  })

  it('passes the attempt a signal that is live, not pre-aborted', async () => {
    let seen: AbortSignal | undefined
    await withDeadline(5_000, undefined, s => { seen = s; return Promise.resolve(1) })
    expect(seen).toBeInstanceOf(AbortSignal)
    expect(seen!.aborted).toBe(false)
  })

  it('propagates the outer signal, so unmount/cancel still aborts the request', async () => {
    const ac = new AbortController()
    const p = withDeadline(5_000, ac.signal, neverArrives)
    ac.abort(new DOMException('unmounted', 'AbortError'))
    await expect(p).rejects.toSatisfy(e => name(e) === 'AbortError')
  })

  it('honours an outer signal that was ALREADY aborted before the call', async () => {
    // A listener added after the fact never fires, so without the up-front
    // check this would sit out the full deadline on an abandoned request.
    const ac = new AbortController()
    ac.abort(new DOMException('gone', 'AbortError'))
    await expect(withDeadline(5_000, ac.signal, neverArrives)).rejects.toSatisfy(
      e => name(e) === 'AbortError')
  })

  it('clears the deadline timer once the attempt resolves', async () => {
    // `AbortSignal.timeout` cannot: it exposes no handle, so a 15s timer
    // outlives every successful call (measured: +10s on a teardown drain).
    const clear = vi.spyOn(globalThis, 'clearTimeout')
    await withDeadline(5_000, undefined, () => Promise.resolve('ok'))
    expect(clear).toHaveBeenCalled()
    clear.mockRestore()
  })

  it('clears the deadline timer when the attempt REJECTS', async () => {
    const clear = vi.spyOn(globalThis, 'clearTimeout')
    await expect(withDeadline(5_000, undefined, () => Promise.reject(new Error('boom'))))
      .rejects.toThrow('boom')
    expect(clear).toHaveBeenCalled()
    clear.mockRestore()
  })

  it('clears the deadline timer when the attempt throws SYNCHRONOUSLY', () => {
    // A sync throw skips the promise `finally`, so it leaks a timer without
    // its own cleanup path.
    const clear = vi.spyOn(globalThis, 'clearTimeout')
    expect(() => withDeadline(5_000, undefined, () => { throw new Error('sync') }))
      .toThrow('sync')
    expect(clear).toHaveBeenCalled()
    clear.mockRestore()
  })

  it('detaches its abort listener from the outer signal once settled', async () => {
    // A react-query signal outlives one fetch, so a listener left attached
    // keeps this controller reachable for the query's whole life.
    const ac = new AbortController()
    const remove = vi.spyOn(ac.signal, 'removeEventListener')
    await withDeadline(5_000, ac.signal, () => Promise.resolve('ok'))
    expect(remove).toHaveBeenCalledWith('abort', expect.any(Function))
    remove.mockRestore()
  })
})
