import { QueryClient, QueryObserver, skipToken } from '@tanstack/react-query'
import { forgetUnobservedMemberThreads, memberThreadQueryKey } from './membersQuery'

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
