import { describe, expect, it, vi } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import {
  applySessionProjection,
  crewLogProjectionsKey,
  readSessionProjectionFrame,
  refetchSessionProjections,
} from '../hooks/useWebSocket'

const SLOT = 'chat-7'
const UNIT = 's-unit-1'

function held(revision: number, unit = UNIT) {
  return {
    unit,
    folds: {
      status: { name: 'status', seq: 3, value: { turns_completed: 1 }, revision },
      usage: { name: 'usage', seq: 3, value: {}, revision },
    },
    resolved: true,
  }
}

function frame(revision: number, overrides: Record<string, unknown> = {}) {
  const read = readSessionProjectionFrame({
    session_id: UNIT,
    slot: SLOT,
    name: 'status',
    seq: 9,
    value: { turns_completed: 2 },
    revision,
    ...overrides,
  })
  if (!read) throw new Error('fixture frame failed the shape check')
  return read
}

function client(data: unknown) {
  const queryClient = new QueryClient()
  queryClient.setQueryData(crewLogProjectionsKey(SLOT), data)
  return queryClient
}

describe('session_projection frames', () => {
  it('applies a newer revision to the held fold and leaves the others', () => {
    const queryClient = client(held(4))
    expect(applySessionProjection(queryClient, frame(5))).toBe('applied')
    const after = queryClient.getQueryData<ReturnType<typeof held>>(crewLogProjectionsKey(SLOT))
    expect(after?.folds.status).toMatchObject({ seq: 9, revision: 5, value: { turns_completed: 2 } })
    expect(after?.folds.usage.revision).toBe(4)
  })

  it('applies a frame naming the slot as dashboard:<slot> to the bare-slot panel cache', () => {
    // The panel reads under the bare slot; the gateway's frame names it
    // scope-qualified. Seeded by the literal key the panel uses.
    const queryClient = new QueryClient()
    queryClient.setQueryData(['crew-log-projections', SLOT], held(4))
    expect(applySessionProjection(queryClient, frame(5, { slot: `dashboard:${SLOT}` }))).toBe('applied')
    const after = queryClient.getQueryData<ReturnType<typeof held>>(['crew-log-projections', SLOT])
    expect(after?.folds.status.revision).toBe(5)
  })

  it('discards a revision at or below the one the tab holds', () => {
    const queryClient = client(held(5))
    expect(applySessionProjection(queryClient, frame(5))).toBe('stale')
    expect(applySessionProjection(queryClient, frame(3))).toBe('stale')
    const after = queryClient.getQueryData<ReturnType<typeof held>>(crewLogProjectionsKey(SLOT))
    expect(after?.folds.status.value).toEqual({ turns_completed: 1 })
  })

  it('refuses a frame from another unit and asks for a read instead', () => {
    const queryClient = client(held(1, 's-unit-0'))
    expect(applySessionProjection(queryClient, frame(9))).toBe('refetch')
    const after = queryClient.getQueryData<ReturnType<typeof held>>(crewLogProjectionsKey(SLOT))
    expect(after?.folds.status.revision).toBe(1)
  })

  it('does not seed across a read in flight', async () => {
    const queryClient = client(held(1))
    let release: (value: unknown) => void = () => undefined
    void queryClient.fetchQuery({
      queryKey: crewLogProjectionsKey(SLOT),
      queryFn: () => new Promise(resolve => { release = resolve }),
      staleTime: 0,
    })
    await vi.waitFor(() => {
      const query = queryClient.getQueryCache().find({ queryKey: crewLogProjectionsKey(SLOT), exact: true })
      expect(query?.state.fetchStatus).toBe('fetching')
    })
    expect(applySessionProjection(queryClient, frame(9))).toBe('refetch')
    release(held(2))
  })

  it('ignores a slot no panel caches, and a malformed frame', () => {
    expect(applySessionProjection(new QueryClient(), frame(2))).toBe('ignored')
    expect(readSessionProjectionFrame({ session_id: UNIT, slot: SLOT, name: 'status', seq: 1, value: {}, revision: 0 })).toBeNull()
    expect(readSessionProjectionFrame({ slot: SLOT, name: 'status', seq: 1, value: {}, revision: 1 })).toBeNull()
    const good = { session_id: UNIT, slot: SLOT, name: 'status', seq: 1, value: {}, revision: 1 }
    expect(readSessionProjectionFrame({ ...good, slot: '' })).toBeNull()
    expect(readSessionProjectionFrame({ ...good, name: 7 })).toBeNull()
    expect(readSessionProjectionFrame({ ...good, seq: Number.NaN })).toBeNull()
    expect(readSessionProjectionFrame({ ...good, value: [] })).toBeNull()
    expect(readSessionProjectionFrame({ ...good, value: null })).toBeNull()
  })

  it('ignores a fold the held read does not carry', () => {
    const queryClient = client(held(1))
    expect(applySessionProjection(queryClient, frame(5, { name: 'timeline' }))).toBe('ignored')
  })
})

describe('refetchSessionProjections', () => {
  it('invalidates the panel read at once when no read is in flight', () => {
    const queryClient = client(held(1))
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    refetchSessionProjections(queryClient, `dashboard:${SLOT}`)
    expect(invalidate).toHaveBeenCalledTimes(1)
    expect(invalidate.mock.calls[0][0]).toEqual({ queryKey: crewLogProjectionsKey(SLOT), exact: true })
  })

  it('waits for an in-flight read to settle, even a failed one, before invalidating', async () => {
    const queryClient = client(held(1))
    let fail: (reason: unknown) => void = () => undefined
    void queryClient
      .fetchQuery({
        queryKey: crewLogProjectionsKey(SLOT),
        queryFn: () => new Promise((_, reject) => { fail = reject }),
        staleTime: 0,
        retry: false,
      })
      .catch(() => undefined)
    await vi.waitFor(() => {
      const query = queryClient.getQueryCache().find({ queryKey: crewLogProjectionsKey(SLOT), exact: true })
      expect(query?.state.fetchStatus).toBe('fetching')
    })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    refetchSessionProjections(queryClient, SLOT)
    expect(invalidate).not.toHaveBeenCalled()
    fail(new Error('read failed'))
    await vi.waitFor(() => expect(invalidate).toHaveBeenCalledTimes(1))
  })
})
