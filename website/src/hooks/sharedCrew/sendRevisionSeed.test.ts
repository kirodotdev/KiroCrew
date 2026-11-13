import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { QueryCache, QueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { baseRevisionFor, noteSendRevision, resetSendRaceForTests, setSendRaceNotice } from '../../api/sendRace'
import type { SendRaceConflictBody } from '../../api/sharedCrew'
import { baseRevisionForPress, ensureSendRevision, resetSendRevisionSeedForTests } from './sendRevisionSeed'
import { applySharedCrewFrame, latestSendState, sharedCrewKeys } from './sharedCrewSocket'

vi.mock('../../api/client', () => ({ api: { sharedCrewSendState: vi.fn() } }))
const sendState = vi.mocked(api.sharedCrewSendState)

const body = (revision: number) => ({ schema_version: 1, session_key: 's', revision, last: null })
const restartConflict: SendRaceConflictBody = {
  error: 'someone else sent first', code: 'send_race', session_key: 's', current_revision: 1,
  winner: { revision: 1, principal: { id: 'p-a', kind: 'human', display: { name: 'A', login: '', captured_at: null } }, message_id: 'm-a', accepted_at: 0 },
}

beforeEach(() => {
  sendState.mockReset()
  resetSendRaceForTests()
  resetSendRevisionSeedForTests()
})
afterEach(() => { vi.useRealTimers() })

describe('ensureSendRevision', () => {
  it('reads nothing when the revision is already known', async () => {
    noteSendRevision('s', 3)
    await ensureSendRevision('s')
    expect(sendState).not.toHaveBeenCalled()
  })

  it('records the revision the read answers', async () => {
    sendState.mockResolvedValue(body(7))
    await ensureSendRevision('s')
    expect(baseRevisionFor('s')).toBe(7)
  })

  it('shares one read between sends waiting on the same session', async () => {
    let answer: (v: ReturnType<typeof body>) => void = () => {}
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    const both = Promise.all([ensureSendRevision('s'), ensureSendRevision('s')])
    answer(body(2))
    await both
    expect(sendState).toHaveBeenCalledTimes(1)
    expect(baseRevisionFor('s')).toBe(2)
  })

  it('joins the read the open session already started instead of reading after the press', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    let answer: (v: ReturnType<typeof body>) => void = () => {}
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    // The open session's own read (the collaboration strip's query), in flight.
    const opened = client.fetchQuery({ queryKey: sharedCrewKeys.sendState('s'), queryFn: () => api.sharedCrewSendState('s') })
    const waiting = ensureSendRevision('s', { client })
    answer(body(0))
    await Promise.all([opened, waiting])
    expect(sendState).toHaveBeenCalledTimes(1)
    expect(baseRevisionFor('s')).toBe(0)
  })

  it('stops waiting at the bound and still records a late answer', async () => {
    vi.useFakeTimers()
    let answer: (v: ReturnType<typeof body>) => void = () => {}
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    const waiting = ensureSendRevision('s', { waitMs: 1000 })
    await vi.advanceTimersByTimeAsync(1000)
    await waiting
    expect(baseRevisionFor('s')).toBeUndefined()
    answer(body(4))
    await vi.waitFor(() => expect(baseRevisionFor('s')).toBe(4))
  })

  it('resolves without a revision when the route answers nothing or fails', async () => {
    sendState.mockResolvedValueOnce(null)
    await ensureSendRevision('s')
    expect(baseRevisionFor('s')).toBeUndefined()
    sendState.mockRejectedValueOnce(new Error('offline'))
    await ensureSendRevision('s')
    expect(baseRevisionFor('s')).toBeUndefined()
  })
})

describe('baseRevisionForPress', () => {
  it('keeps the revision known at the press even after a later frame raised it', async () => {
    noteSendRevision('s', 2)
    const atPress = baseRevisionFor('s')
    noteSendRevision('s', 3)
    expect(await baseRevisionForPress('s', atPress)).toBe(2)
    expect(sendState).not.toHaveBeenCalled()
  })

  it('reads the session when nothing was known at the press', async () => {
    sendState.mockResolvedValue(body(5))
    expect(await baseRevisionForPress('s', undefined)).toBe(5)
  })

  it('bases an unknown press on its read even when a frame lands while the read is pending', async () => {
    let answer: (v: ReturnType<typeof body>) => void = () => {}
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    const base = baseRevisionForPress('s', undefined)
    noteSendRevision('s', 1)
    answer(body(0))
    expect(await base).toBe(0)
  })

  it('keeps the press-time snapshot alive while a winning frame updates the cache', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    let answer!: (value: ReturnType<typeof body>) => void
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    const opened = client.fetchQuery({ queryKey: sharedCrewKeys.sendState('s'), queryFn: () => api.sharedCrewSendState('s'), structuralSharing: latestSendState })
    const base = baseRevisionForPress('s', undefined, { client })
    try {
      applySharedCrewFrame(client, { type: 'shared_crew.send_state', ...body(1) })
      expect(client.isFetching({ queryKey: sharedCrewKeys.sendState('s') })).toBe(1)
      expect(client.getQueryData(sharedCrewKeys.sendState('s'))).toEqual(body(1))
      answer(body(0))
      expect(await base).toBe(0)
      await opened
      expect(sendState).toHaveBeenCalledTimes(1)
      expect(client.getQueryData(sharedCrewKeys.sendState('s'))).toEqual(body(1))
      expect(baseRevisionFor('s')).toBe(1)
    } finally {
      client.clear()
    }
  })

  it('does not restore a pre-restart read that settles after a conflict resets the cache', async () => {
    const client = new QueryClient()
    let answer!: (value: ReturnType<typeof body>) => void
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    try {
      const base = baseRevisionForPress('s', undefined, { client })
      noteSendRevision('s', 10)
      setSendRaceNotice('s', restartConflict, client)
      answer(body(10))
      await base
      expect(baseRevisionFor('s')).toBe(1)
      expect(client.getQueryData(sharedCrewKeys.sendState('s'))).toBeUndefined()
    } finally {
      client.clear()
    }
  })

  it('does not seed from a completed snapshot when a conflict resets it before the continuation', async () => {
    const client = new QueryClient({ queryCache: new QueryCache({
      onSuccess: () => setSendRaceNotice('s', restartConflict, client),
    }) })
    sendState.mockResolvedValue(body(10))
    try {
      expect(await baseRevisionForPress('s', undefined, { client })).toBe(10)
      expect(client.getQueryData(sharedCrewKeys.sendState('s'))).toBeUndefined()
      expect(baseRevisionFor('s')).toBe(1)
    } finally {
      client.clear()
    }
  })

  it('bases an unknown press on its read even when a frame landed before the helper ran', async () => {
    let answer: (v: ReturnType<typeof body>) => void = () => {}
    sendState.mockReturnValue(new Promise(resolve => { answer = resolve }))
    const atPress = baseRevisionFor('s')
    noteSendRevision('s', 1)
    const base = baseRevisionForPress('s', atPress)
    answer(body(0))
    expect(await base).toBe(0)
  })
})
