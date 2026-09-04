/**
 * Tests for rewindWithRollback — the helper that wraps api.rewind with a
 * rollback callback. Covers both the success and failure paths so the catch
 * branch is exercised in unit tests rather than requiring a full ChatPage
 * mount + click + reject simulation.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { rewindWithRollback } from '../lib/rewindCall'

describe('rewindWithRollback', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>
  let warnSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  afterEach(() => {
    fetchSpy?.mockRestore()
    warnSpy.mockRestore()
  })

  it('does not invoke rollback when api.rewind resolves', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const rollback = vi.fn()
    await rewindWithRollback('slot-abc', 'ts-1', 'edited', rollback)
    expect(rollback).not.toHaveBeenCalled()
    expect(warnSpy).not.toHaveBeenCalled()
  })

  it('invokes rollback and warns when api.rewind rejects', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: 'slot is running' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const rollback = vi.fn()
    await rewindWithRollback('slot-abc', 'ts-1', 'edited', rollback)
    expect(rollback).toHaveBeenCalledOnce()
    expect(warnSpy).toHaveBeenCalledWith('rewind failed', expect.any(Error))
  })

  it('invokes rollback when fetch itself throws (network error)', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('network down'))
    const rollback = vi.fn()
    await rewindWithRollback('slot-abc', 'ts-1', 'edited', rollback)
    expect(rollback).toHaveBeenCalledOnce()
    expect(warnSpy).toHaveBeenCalled()
  })
})
