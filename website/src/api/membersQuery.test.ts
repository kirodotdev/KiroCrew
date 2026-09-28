import { QueryClient, QueryObserver, skipToken } from '@tanstack/react-query'
import {
  forgetUnobservedMemberThreads,
  memberProjectionsQuery,
  memberProjectionsQueryKey,
  memberThreadQueryKey,
} from './membersQuery'
import { memberProjectionStore } from '../state/memberProjectionStore'

/* The websocket hook forgets cached member-thread outcomes on reconnect so a
 * return visit after a gateway restart waits for the thread endpoint again.
 * The Crew Members page reads the OPEN member's entry through a `skipToken`
 * query, which react-query classifies as inactive — so the sweep must count
 * observers, never use `type: 'inactive'`: clearing the observed entry would
 * unmount the mounted ChatPane mid-reconnect and drop the draft typed into it
 * (PR #9442 review finding). */
describe('forgetUnobservedMemberThreads', () => {
  it('drops entries nobody observes and keeps the one a skipToken reader is subscribed to', () => {
    const qc = new QueryClient()
    qc.setQueryData(memberThreadQueryKey('radar'), { slot_key: 'member-radar' })
    qc.setQueryData(memberThreadQueryKey('fixer'), { slot_key: 'member-fixer' })
    // Exactly how MembersPage reads the open member: a disabled (skipToken)
    // observer — react-query's `type: 'inactive'` filter would match it.
    const observer = new QueryObserver(qc, { queryKey: memberThreadQueryKey('radar'), queryFn: skipToken })
    const unsubscribe = observer.subscribe(() => {})
    try {
      forgetUnobservedMemberThreads(qc)
      expect(qc.getQueryData(memberThreadQueryKey('radar'))).toEqual({ slot_key: 'member-radar' })
      expect(qc.getQueryData(memberThreadQueryKey('fixer'))).toBeUndefined()
    } finally {
      unsubscribe()
    }
  })

  it('leaves other caches alone', () => {
    const qc = new QueryClient()
    qc.setQueryData(['kirocrew-agents', 'members-roster'], [])
    qc.setQueryData(memberThreadQueryKey('radar'), { slot_key: 'member-radar' })
    forgetUnobservedMemberThreads(qc)
    expect(qc.getQueryData(['kirocrew-agents', 'members-roster'])).toEqual([])
    expect(qc.getQueryData(memberThreadQueryKey('radar'))).toBeUndefined()
  })
})

/* The roster list carries the `roster` view alone, because that is the only one
 * a list ROW paints. The drawer paints activity, wake and driving, so it reads
 * the whole block through this query and seeds the same store
 * `useMemberProjection` already reads -- without that seed, a member who has not
 * moved since the gateway started opens to an empty drawer. */
describe('memberProjectionsQuery', () => {
  afterEach(() => {
    memberProjectionStore.clear()
  })

  it('seeds every view the drawer paints, not just the one the list row carries', async () => {
    const block = {
      asOfSeq: 12,
      values: {
        roster: { name: 'Radar', slug: 'radar' },
        activity: { recent: [], today: 1, week: 2 },
        wake: { state: 'armed' },
        driving: { open: ['chat-1'] },
      },
    }
    const q = memberProjectionsQuery('radar', 'Radar')
    // `select` is where the seed happens, so the store is fed by the same code
    // path react-query runs on a successful read.
    expect(q.select!(block)).toBe(block)
    for (const key of ['roster', 'activity', 'wake', 'driving']) {
      expect(memberProjectionStore.get('radar', key)).toEqual(block.values[key])
    }
  })

  it('is keyed under the roster scope so one invalidation revalidates both', () => {
    // A crew edited in another tab invalidates ['kirocrew-agents']; the drawer
    // must not keep showing what the log held before that save.
    expect(memberProjectionsQueryKey('radar', 'Radar')[0]).toBe('kirocrew-agents')
    // Keyed by exact NAME as well as slug: slugs are lossy, so two names can
    // share one log and the server answers 409 rather than guessing.
    expect(memberProjectionsQueryKey('radar', 'Radar')).not.toEqual(
      memberProjectionsQueryKey('radar', 'Radar_2'),
    )
  })

  it('does not seed a negative sequence, so a read it cannot stand behind mutes nothing', () => {
    // In the store a negative asOfSeq is the roster's refusal to attribute a
    // slug: it clears that slug's whole cache and mutes the live frames that
    // follow. This route never means that by it -- a member with no log yet
    // answers an empty baseline at a real sequence, and every case the roster
    // refuses answers 409 -- so a negative here is only a read it could not
    // stand behind, and blanking a correctly displaying row is the wrong answer.
    memberProjectionStore.seed('radar', { roster: { name: 'Radar', slug: 'radar' } }, 9)
    expect(memberProjectionStore.get('radar', 'roster')).toEqual({ name: 'Radar', slug: 'radar' })

    const q = memberProjectionsQuery('radar', 'Radar')
    q.select!({ asOfSeq: -1, values: {} })

    expect(memberProjectionStore.get('radar', 'roster')).toEqual({ name: 'Radar', slug: 'radar' })
    expect(memberProjectionStore.has('radar')).toBe(true)
  })

  it('cannot lift a roster refusal, even re-run over a block cached before it', () => {
    // React Query re-runs a select over whatever block is cached, and this query is
    // built inline in the page so its select has a new identity on every render. A
    // block held from before the roster refused the slug would therefore be replayed
    // AFTER the refusal: through `seed` that restores the pre-failure values and
    // re-admits live frames, while `data` stays defined so no error state shows.
    // Attribution is the roster's to decide, so this route contributes values and
    // never adjudicates.
    const cached = { asOfSeq: 12, values: { roster: { name: 'Radar', slug: 'radar' } } }
    const q = memberProjectionsQuery('radar', 'Radar')
    q.select!(cached)
    expect(memberProjectionStore.get('radar', 'roster')).toEqual(cached.values.roster)

    // The roster now refuses the slug: an unprovable log, a collision, a failed read.
    memberProjectionStore.seed('radar', {}, -1)
    expect(memberProjectionStore.get('radar', 'roster')).toBeUndefined()

    // A re-render replays the cached block through the same select.
    q.select!(cached)
    expect(memberProjectionStore.get('radar', 'roster')).toBeUndefined()

    // And the refusal still holds against live frames, which is what it is for.
    memberProjectionStore.apply('radar', 'wake', { state: 'armed' }, 99)
    expect(memberProjectionStore.get('radar', 'wake')).toBeUndefined()
  })

  it('a logless member seeded from the roster still receives its first live frame', () => {
    // The regression the empty baseline exists to prevent. Every member starts
    // without a log, so if the roster reported that as a refusal to attribute,
    // the first roster read on a fresh install would mute every row on the page
    // and the frame written the moment a member is first active would be dropped.
    // 0 is the log's own empty position, and a recorded event starts at 1.
    memberProjectionStore.seed('fresh', {}, 0)
    expect(memberProjectionStore.has('fresh')).toBe(false)

    memberProjectionStore.apply('fresh', 'roster', { name: 'Fresh' }, 1)
    expect(memberProjectionStore.get('fresh', 'roster')).toEqual({ name: 'Fresh' })
  })
})
