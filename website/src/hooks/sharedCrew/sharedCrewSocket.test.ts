import { describe, it, expect, vi, afterEach } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import type { AcceptedWork } from '../../api/sharedCrew'
import type { WorkListResponse } from '../../api/sharedCrewUi'
import { applySharedCrewFrame, latestSendState, onSharedCrewSocketOpen, sendActivity, setSharedCrewSender, sharedCrewKeys } from './sharedCrewSocket'

const work = (revision: number, state: AcceptedWork['state']): AcceptedWork => ({
  work_id: 'w1', session_key: 's1', operation: 'chat_turn', state, revision,
  accepted_at: 1, expires_at: 2, deadline_at: null, reason: null,
})
const frame = (w: AcceptedWork) => ({ type: 'shared_crew.work', schema_version: 1, work: w })

afterEach(() => setSharedCrewSender(null))

describe('shared crew frames', () => {
  it('folds work frames by id and revision, so a replay never duplicates', () => {
    const qc = new QueryClient()
    applySharedCrewFrame(qc, frame(work(1, 'queued')))
    applySharedCrewFrame(qc, frame(work(2, 'running')))
    applySharedCrewFrame(qc, frame(work(1, 'queued')))
    const items = qc.getQueryData<WorkListResponse>(sharedCrewKeys.work('s1'))?.items
    expect(items).toHaveLength(1)
    expect(items?.[0].state).toBe('running')
  })

  it('files a chat work frame under the tab key the work list reads', () => {
    const qc = new QueryClient()
    const chat = { ...work(1, 'queued'), session_key: 'dashboard:tab-1' }
    applySharedCrewFrame(qc, { ...frame(chat), slot_key: 'tab-1' })
    expect(qc.getQueryData<WorkListResponse>(sharedCrewKeys.work('tab-1'))?.items[0].state).toBe('queued')
    expect(qc.getQueryData<WorkListResponse>(sharedCrewKeys.work('dashboard:tab-1'))?.items).toHaveLength(1)
  })

  it('an HTTP read that lands before a newer frame is overtaken, not kept', () => {
    const qc = new QueryClient()
    qc.setQueryData<WorkListResponse>(sharedCrewKeys.work('s1'), { items: [work(1, 'queued')] })
    applySharedCrewFrame(qc, frame(work(4, 'completed')))
    expect(qc.getQueryData<WorkListResponse>(sharedCrewKeys.work('s1'))?.items[0].state).toBe('completed')
  })

  it('replaces presence as a whole value', () => {
    const qc = new QueryClient()
    const entry = { principal: { id: 'p_a', kind: 'human', display: { name: 'A', login: '', captured_at: null } }, session_key: 's1', activity: 'typing', connections: 2, expires_at: 9 }
    applySharedCrewFrame(qc, { type: 'shared_crew.presence', schema_version: 1, session_key: 's1', entries: [entry] })
    applySharedCrewFrame(qc, { type: 'shared_crew.presence', schema_version: 1, session_key: 's1', entries: [] })
    expect(qc.getQueryData<{ entries: unknown[] }>(sharedCrewKeys.presence('s1'))?.entries).toEqual([])
  })

  it.each([
    { key: sharedCrewKeys.typingLease('s1'), next: { holder: null, holder_ids: [], expires_at: null, ended: 'empty' }, type: 'shared_crew.typing_lease' },
    { key: sharedCrewKeys.presence('s1'), next: { entries: [] }, type: 'shared_crew.presence' },
    { key: sharedCrewKeys.sendState('s1'), next: { revision: 2, last: null }, type: 'shared_crew.send_state' },
    { key: sharedCrewKeys.capacity, next: { limiting_scope: 'crew' }, type: 'shared_crew.capacity' },
  ])('keeps $type authoritative over an outstanding HTTP snapshot', async ({ key, next, type }) => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    let answer!: (value: object) => void
    const snapshot = new Promise<object>(resolve => { answer = resolve })
    const read = qc.fetchQuery({ queryKey: key, queryFn: () => snapshot, structuralSharing: type === 'shared_crew.send_state' ? latestSendState : true }).catch(() => undefined)
    expect(qc.isFetching({ queryKey: key })).toBe(1)
    applySharedCrewFrame(qc, { type, schema_version: 1, session_key: 's1', ...next, capacity: next })
    answer(type === 'shared_crew.send_state' ? { revision: 0, last: null } : { stale: true })
    await read
    expect(qc.getQueryData(key)).toMatchObject(next)
    expect(qc.getQueryState(key)?.status).toBe('success')
    expect(qc.isFetching({ queryKey: key })).toBe(0)
    qc.clear()
  })

  it.each(['work', 'decision'] as const)('re-reads an interrupted %s list without losing its other rows', async kind => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const key = kind === 'work' ? sharedCrewKeys.work('s1') : sharedCrewKeys.decisions('s1')
    const next = kind === 'work' ? work(2, 'running') : { decision_id: 'd1', session_key: 's1', revision: 2 }
    const other = kind === 'work' ? { ...work(1, 'queued'), work_id: 'w2' } : { decision_id: 'd2', session_key: 's1', revision: 1 }
    let answer!: (value: object) => void
    const snapshot = new Promise<object>(resolve => { answer = resolve })
    const current = { schema_version: 1, items: [next, other] }
    const read = vi.fn().mockReturnValueOnce(snapshot).mockResolvedValue(current)
    const observer = new QueryObserver(qc, { queryKey: key, queryFn: read })
    let ready!: () => void
    const updated = new Promise<void>(resolve => { ready = resolve })
    const unsubscribe = observer.subscribe(result => { if (result.isSuccess && result.data.items.length === 2) ready() })
    try {
      expect(read).toHaveBeenCalledTimes(1)
      applySharedCrewFrame(qc, { type: `shared_crew.${kind}`, schema_version: 1, [kind]: next })
      expect(read).toHaveBeenCalledTimes(2)
      answer({ items: [] })
      await updated
      expect(qc.getQueryData(key)).toEqual(current)
    } finally {
      unsubscribe()
      qc.clear()
    }
  })

  it.each((['work', 'decision'] as const).flatMap(kind =>
    (['absent', 'unavailable', 'invalidated'] as const).map(cache => ({ kind, cache })),
  ))('fetches a complete $kind list after deltas reach an $cache cache', async ({ kind, cache }) => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const keys = kind === 'work' ? [sharedCrewKeys.work('s1'), sharedCrewKeys.work('tab-1')] : [sharedCrewKeys.decisions('s1')]
    const next = kind === 'work' ? work(2, 'running') : { decision_id: 'd1', session_key: 's1', revision: 2 }
    const other = kind === 'work' ? { ...work(1, 'queued'), work_id: 'w2' } : { decision_id: 'd2', session_key: 's1', revision: 1 }
    const update = { type: `shared_crew.${kind}`, schema_version: 1, slot_key: 'tab-1', [kind]: next }
    const current = { schema_version: 1, items: [next, other] }
    const unsubscribes: (() => void)[] = []
    try {
      for (const key of keys) {
        if (cache === 'unavailable') qc.setQueryData(key, null)
        if (cache === 'invalidated') {
          qc.setQueryData(key, { items: [] })
          await qc.invalidateQueries({ queryKey: key, exact: true, refetchType: 'none' })
        }
      }
      applySharedCrewFrame(qc, update)
      applySharedCrewFrame(qc, update)
      for (const key of keys) {
        expect(qc.getQueryState(key)?.isInvalidated).toBe(true)
        const read = vi.fn().mockResolvedValue(current)
        const observer = new QueryObserver(qc, { queryKey: key, queryFn: read })
        unsubscribes.push(observer.subscribe(() => {}))
        await vi.waitFor(() => expect(qc.getQueryData(key)).toEqual(current))
        expect(read).toHaveBeenCalledTimes(1)
        expect(qc.getQueryState(key)?.isInvalidated).toBe(false)
        applySharedCrewFrame(qc, update)
        expect(read).toHaveBeenCalledTimes(1)
        expect(qc.getQueryData(key)).toEqual(current)
        expect(qc.getQueryState(key)?.isInvalidated).toBe(false)
      }
    } finally {
      unsubscribes.forEach(unsubscribe => unsubscribe())
      qc.clear()
    }
  })

  it.each(['presence', 'work'] as const)('refreshes People after %s frames, including inactive queries', async kind => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    qc.setQueryData(sharedCrewKeys.people, { people: [] })
    const read = vi.fn().mockResolvedValue({ people: ['collaborator'] })
    const observer = new QueryObserver(qc, { queryKey: sharedCrewKeys.people, queryFn: read })
    const unsubscribe = observer.subscribe(() => {})
    try {
      expect(read).not.toHaveBeenCalled()
      const update = kind === 'work' ? frame(work(1, 'running')) : { type: 'shared_crew.presence', session_key: 's1', entries: [] }
      applySharedCrewFrame(qc, update)
      await vi.waitFor(() => expect(qc.getQueryData(sharedCrewKeys.people)).toEqual({ people: ['collaborator'] }))
      expect(read).toHaveBeenCalledTimes(1)
      unsubscribe()
      applySharedCrewFrame(qc, update)
      expect(qc.getQueryState(sharedCrewKeys.people)?.isInvalidated).toBe(true)
      expect(read).toHaveBeenCalledTimes(1)
    } finally {
      unsubscribe()
      qc.clear()
    }
  })

  it('ignores malformed frames', () => {
    const qc = new QueryClient()
    applySharedCrewFrame(qc, null)
    applySharedCrewFrame(qc, { type: 'shared_crew.work' })
    applySharedCrewFrame(qc, { type: 'shared_crew.decision', decision: {} })
    expect(qc.getQueryCache().getAll()).toHaveLength(0)
  })

  it('a reopened socket re-reads every Shared Crew query', () => {
    const qc = new QueryClient()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    onSharedCrewSocketOpen(qc)
    expect(spy).toHaveBeenCalledWith({ queryKey: ['shared-crew'] }, { cancelRefetch: false })
  })
})

describe('activity frames', () => {
  it('carry a session and an activity, never a principal or text', () => {
    const sent: object[] = []
    setSharedCrewSender(p => sent.push(p))
    expect(sendActivity('s1', 'typing')).toBe(true)
    expect(sent).toEqual([{ type: 'shared_crew.activity', session_key: 's1', activity: 'typing' }])
  })

  it('send nothing without a socket or a session', () => {
    expect(sendActivity('s1', 'viewing')).toBe(false)
    setSharedCrewSender(() => { throw new Error('should not send') })
    expect(sendActivity('', 'viewing')).toBe(false)
  })
})
