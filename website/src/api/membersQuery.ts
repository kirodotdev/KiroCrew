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

export const membersRosterQuery = {
  queryKey: MEMBERS_ROSTER_QUERY_KEY,
  // The mark is taken BEFORE the request goes out, and the reconcile runs here
  // rather than in `select`, for two separate reasons. A contributed row written
  // while this read is in flight is NEWER than its answer, so absence from that
  // answer says nothing about it — a mark taken once the response has arrived
  // already counts such a row as old and deletes it. And only a real fetch is
  // authoritative: `select` also runs on a cache read, which cannot say a row is
  // gone.
  queryFn: async (): Promise<MemberRosterRow[]> => {
    const mark = memberProjectionStore.mark()
    const rows = (await api.members()).members
    for (const row of rows) {
      if (row.projections) {
        // Stamp the mark onto the block so `select` can hand it to seed(). It
        // travels with the answer rather than sitting in a store field because
        // `select` runs on a CACHE read too, and the mark that applies there
        // belongs to the fetch that produced those rows.
        row.projections.clientMark = mark
        memberProjectionStore.reconcileContributed(
          row.slug,
          Object.keys(row.projections.values),
          mark,
        )
      }
    }
    return rows
  },
  staleTime: MEMBERS_ROSTER_STALE_MS,
  // Seed the per-member projection store from each row's baseline block BEFORE
  // the page renders rows — `select` runs synchronously on the query result,
  // so the first paint already reads pushed values via useMemberProjection.
  // seed() applies at asOfSeq through the store's higher-seq-wins rule, so a
  // live frame that raced ahead of this baseline keeps winning. Rows pass
  // through unchanged.
  select: (rows: MemberRosterRow[]): MemberRosterRow[] => {
    for (const row of rows) {
      if (row.projections) {
        // `seqs` / `schemas` / `stateVersions` are present only for CONTRIBUTED
        // rows: such a row's seq is the contributor's own fold position rather
        // than this response's asOfSeq, and seeding it at asOfSeq would make
        // higher-seq-wins drop the contributor's next live push.
        //
        // `clientMark` is the mark the queryFn stamped before the request went
        // out. It decides the one case higher-seq-wins cannot: a contributed key
        // a teardown frame withdrew while this answer was in flight is absent
        // from the store, so there is no held row to compare against and the
        // withdrawn card would be seeded straight back onto the page.
        memberProjectionStore.seed(
          row.slug,
          row.projections.values,
          row.projections.asOfSeq,
          row.projections.seqs,
          row.projections.schemas,
          row.projections.stateVersions,
          row.projections.clientMark,
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
