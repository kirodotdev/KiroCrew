import { QueryClient, QueryObserver, skipToken } from '@tanstack/react-query'
import { api, type MemberRosterRow } from './client'
import {
  forgetUnobservedMemberThreads,
  memberThreadQueryKey,
  membersRosterQuery,
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

/* The roster read is asynchronous and the socket is live while it is in flight, so
 * the answer is seeded against the revision the REQUEST was issued at rather than
 * the moment it arrived. Without that, a deletion landing mid-request is undone by
 * an answer that still carries the key. */
describe('the roster answer is seeded at the revision its read was issued', () => {
  const rowFor = (slug: string, seq: number): MemberRosterRow =>
    ({
      name: slug,
      slug,
      slot_key: '',
      running: false,
      projections: {
        asOfSeq: 12,
        values: { 'demo/card': { n: 1 } },
        seqs: { 'demo/card': seq },
      },
    }) as MemberRosterRow

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('a deletion landing during the request is not undone by the answer', async () => {
    const slug = 'issued-rev-alice'
    memberProjectionStore.seed(slug, { 'demo/card': { n: 1 } }, 12, {}, { 'demo/card': 7 })
    expect(memberProjectionStore.get(slug, 'demo/card')).toEqual({ n: 1 })

    vi.spyOn(api, 'members').mockImplementation(async () => {
      // The app deletes the key while this request is on the wire.
      memberProjectionStore.apply(slug, 'demo/card', null, 8)
      return { members: [rowFor(slug, 7)] } as Awaited<ReturnType<typeof api.members>>
    })

    const rows = await membersRosterQuery.queryFn()
    membersRosterQuery.select(rows)
    expect(memberProjectionStore.get(slug, 'demo/card')).toBeUndefined()
  })

  it('an answer with nothing newer against it still seeds its keys', async () => {
    // CONTROL. The refusal above must come from the deletion's own revision, not
    // from baselines having stopped seeding.
    const slug = 'issued-rev-bob'
    vi.spyOn(api, 'members').mockImplementation(
      async () => ({ members: [rowFor(slug, 7)] }) as Awaited<ReturnType<typeof api.members>>,
    )

    const rows = await membersRosterQuery.queryFn()
    membersRosterQuery.select(rows)
    expect(memberProjectionStore.get(slug, 'demo/card')).toEqual({ n: 1 })
  })
})
