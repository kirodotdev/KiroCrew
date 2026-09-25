import type { QueryClient } from '@tanstack/react-query'
import { api, type MemberRosterRow } from './client'
import { memberProjectionStore } from '../state/memberProjectionStore'
import type { ErrorReport } from '../utils/errorReport'

/**
 * The Crew Members page's React Query definitions (issue #9418).
 *
 * One module so the page, its tests and any future consumer (the sidebar's
 * Crew section, a command-palette provider) spell each key exactly once —
 * two inline spellings with different options would diverge silently.
 *
 * Roster freshness follows the dashboard's sanctioned pattern (see
 * queryClient.ts): pushed invalidation first, a finite staleTime as the floor.
 * The roster is a projection of the crew registry, so its key lives UNDER
 * `['kirocrew-agents']` — the prefix useWebSocket invalidates on every
 * `refresh` frame and the crew editor invalidates after a save — and a crew
 * created, renamed or starred anywhere reaches this cache without the page
 * knowing who wrote. The second segment keeps it a distinct entry: the
 * registry query returns `{ agents, default_agent }`, this one returns rows,
 * and sharing one key would let whichever mounted first decide the shape.
 */
export const MEMBERS_ROSTER_QUERY_KEY = ['kirocrew-agents', 'members-roster'] as const

/** Finite so a return to the page after this long refetches in the
 *  background (and on focus) while the cached roster renders immediately —
 *  the list is never blank on a revisit, only silently refreshed. Matches
 *  the other registry projections (`default-agent`). */
const MEMBERS_ROSTER_STALE_MS = 30_000

/**
 * When each roster array's read was ISSUED, on the projection store's own
 * revision counter.
 *
 * Written before the request is awaited and read when the answer is seeded,
 * because those are different moments and the socket is live between them. A
 * deletion frame landing in that gap removes a row the in-flight baseline still
 * carries, and a baseline that cannot say how old it is puts the row back. The
 * store's counter is the one axis both can be placed on: a contributed key's
 * `seq` is its app's fold position, a different counter from the response's
 * `asOfSeq`, so neither answers this.
 *
 * Keyed by the array itself, so the age belongs to that exact answer. An array
 * this map does not know — an optimistic `setQueryData` edit builds a new one —
 * reads as {@link UNKNOWN_ISSUE_REV}.
 */
const ISSUED_AT_REV = new WeakMap<readonly MemberRosterRow[], number>()

/** Older than any drop the store can hold, so a baseline of unknown age
 *  restores nothing that was dropped and evicts nothing either. Both
 *  directions are safe: a real baseline follows and answers for the slug. */
const UNKNOWN_ISSUE_REV = -1

export const membersRosterQuery = {
  queryKey: MEMBERS_ROSTER_QUERY_KEY,
  queryFn: async (): Promise<MemberRosterRow[]> => {
    // Before the await, not after: every projection frame the socket delivers
    // while this request is in flight lands at a higher revision than this, and
    // that difference is what ranks this baseline against a deletion.
    const issuedAtRev = memberProjectionStore.revision()
    const rows = (await api.members()).members
    ISSUED_AT_REV.set(rows, issuedAtRev)
    return rows
  },
  staleTime: MEMBERS_ROSTER_STALE_MS,
  // Seed the per-member projection store from each row's baseline block BEFORE
  // the page renders rows — `select` runs synchronously on the query result,
  // so the first paint already reads pushed values via useMemberProjection.
  // seed() applies at asOfSeq through the store's higher-seq-wins rule, so a
  // live frame that raced ahead of this baseline keeps winning, and it ranks
  // the whole block against drops by the revision above. Rows pass through
  // unchanged.
  select: (rows: MemberRosterRow[]): MemberRosterRow[] => {
    const issuedAtRev = ISSUED_AT_REV.get(rows) ?? UNKNOWN_ISSUE_REV
    for (const row of rows) {
      if (row.projections) {
        memberProjectionStore.seed(
          row.slug,
          row.projections.values,
          row.projections.asOfSeq,
          row.projections.stateVersions,
          row.projections.seqs,
          issuedAtRev,
        )
      }
    }
    return rows
  },
}

/** Recent-activity pointers for one member's drawer. Keyed by the exact
 *  member NAME as well as the slug — slugs are lossy, and the backend's
 *  member filter exists precisely so two names sharing a slug keep distinct
 *  histories. */
export const memberActivityQueryKey = (slug: string, member: string) =>
  ['member-activity', slug, member] as const

/** Query key for a member's briefing markdown (Notes tab). Keyed by slug and
 *  exact name: slugs are lossy, so two names sharing a slug have distinct
 *  briefings. Nested under `['kirocrew-agents']` like the roster: whether the
 *  briefing is one crewmate's to show and edit is a fact about the REGISTRY
 *  (a second crew deriving the same slug turns the read into a 409), so the
 *  same `refresh` frame that refetches the roster must revalidate cached
 *  notes -- otherwise a panel opened before the collision keeps stale text
 *  and an enabled Edit over a file that is now shared. */
export const memberBriefingQueryKey = (slug: string, member: string) =>
  ['kirocrew-agents', 'member-briefing', slug, member] as const

/**
 * The outcome of the last thread open for one member — what
 * POST /api/members/{slug}/thread answered. Written by the open mutation
 * (`setQueryData`), never fetched by a queryFn: the endpoint is a write (the
 * idempotent creator/repairer of member slots, and the ONLY one), so it goes
 * through `useMutation` on every open, while its answer is cached here so a
 * return to a member mounts the thread at once and the re-POST repairs in
 * the background. Keyed by name, not slug: a lossy-slug collision is a
 * per-NAME fact (`Oncall` collides, `oncall` owns the thread).
 */
export const memberThreadQueryKey = (member: string) => ['member-thread', member] as const

/**
 * Forget every member-thread outcome NOBODY is looking at. Called by the
 * websocket hook on reconnect: these entries are trust-sensitive — the key
 * they hold is what the DM composer sends into — and a dropped socket is the
 * one client-side sign the gateway may have restarted and dropped the slot
 * behind a key. (Their idle lifetime is otherwise react-query's default
 * gcTime; nothing here needs a different one.) The page re-confirms the
 * observed entry on that same reconnect.
 *
 * Counts observers, deliberately NOT react-query's `type: 'inactive'` filter:
 * the page reads these entries through a `skipToken` query, which react-query
 * classifies as inactive (an observer with `enabled: false` does not make a
 * query active), so that filter would also clear the OPEN member's entry —
 * unmounting the mounted ChatPane mid-reconnect and dropping the draft typed
 * into it. The open member's entry stays; the page re-confirms it with the
 * repair POST on the same reconnect.
 */
export function forgetUnobservedMemberThreads(queryClient: QueryClient): void {
  queryClient.removeQueries({
    queryKey: ['member-thread'],
    predicate: (query) => query.getObserversCount() === 0,
  })
}

export interface MemberThreadOutcome {
  /** The slot the thread mounts on; '' while no answer has confirmed one.
   *  A failed re-POST keeps the previous key (the cached thread stays up
   *  under the error line); a collision clears it (nothing here is safe to
   *  mount). */
  slot_key: string
  /** Set when the slug's thread belongs to another crew (first-bound-wins):
   *  the name of the crew that owns it. */
  collision?: string
  /** Set when the last POST failed. */
  failed?: boolean
  /** Redacted diagnostics for this member's failed open or repair. */
  errorReport?: ErrorReport
}
