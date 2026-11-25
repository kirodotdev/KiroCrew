/**
 * Tests for rewindWithRollback — the helper that wraps api.rewind with a
 * rollback callback. Covers both the success and failure paths so the catch
 * branch is exercised in unit tests rather than requiring a full ChatPage
 * mount + click + reject simulation.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/apiError'
import { rewindWithRollback } from '../lib/rewindCall'

const jsonResponse = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

describe('rewindWithRollback', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  afterEach(() => {
    fetchSpy?.mockRestore()
  })

  it('does not invoke rollback when api.rewind resolves', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(200, { ok: true }))
    const rollback = vi.fn()
    await rewindWithRollback('slot-abc', 'ts-1', 'edited', rollback)
    expect(rollback).not.toHaveBeenCalled()
  })

  it('rolls back with the ApiError when the readiness gate refuses (503)', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(503, {
      error: 'Kiro CLI setup or sign-in is required before starting a session.',
      code: 'kiro_prerequisite_required',
    }))
    const rollback = vi.fn()
    await rewindWithRollback('slot-abc', 'ts-1', 'edited', rollback)
    expect(rollback).toHaveBeenCalledOnce()
    const err = rollback.mock.calls[0][0]
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(503)
    expect((err as ApiError).message).toContain('Kiro CLI setup or sign-in is required')
  })

  it('rolls back with the ApiError when the slot is running (409)', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(409, { error: 'slot is running' }))
    const rollback = vi.fn()
    await rewindWithRollback('slot-abc', 'ts-1', 'edited', rollback)
    expect(rollback).toHaveBeenCalledOnce()
    const err = rollback.mock.calls[0][0]
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
    expect((err as ApiError).message).toContain('slot is running')
  })

  it('rolls back with the error when fetch itself throws (network error)', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('network down'))
    const rollback = vi.fn()
    await rewindWithRollback('slot-abc', 'ts-1', 'edited', rollback)
    expect(rollback).toHaveBeenCalledOnce()
    expect(rollback.mock.calls[0][0]).toBeInstanceOf(Error)
    expect((rollback.mock.calls[0][0] as Error).message).toContain('network down')
  })
})
