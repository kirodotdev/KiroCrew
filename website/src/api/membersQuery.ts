import type { QueryClient } from '@tanstack/react-query'
import { api, type MemberRosterRow } from './client'
import { memberProjectionStore } from '../state/memberProjectionStore'
import type { ProjectionsBlock } from '../state/memberProjectionTypes'
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
  queryFn: async (): Promise<MemberRosterRow[]> => (await api.members()).members,
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
        memberProjectionStore.seed(row.slug, row.projections.values, row.projections.asOfSeq)
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

/** The open member's folded projection views. Keyed by slug and exact name for
 *  the same reason the activity read is: the server answers 409 rather than
 *  guessing which of two colliding names a slug-keyed log belongs to.
 *
 *  Nested under `['kirocrew-agents']` like the roster, so the same `refresh`
 *  frame and the same post-save invalidation that revalidate the roster also
 *  revalidate the open member's views -- a crew edited in another tab must not
 *  leave the drawer showing what the log held before the save. */
/** Prefix every per-member projections query is keyed under. Exported because the
 *  `members_subscribed` repair has to invalidate these queries as well as the
 *  roster: a torn-tail truncation drops EVERY held key for a slug, and a roster row
 *  restores only the view it carries, so the drawer's other views come back from
 *  their own read or not at all. One prefix, so the two sides cannot drift. */
export const MEMBER_PROJECTIONS_QUERY_PREFIX = ['kirocrew-agents', 'member-projections'] as const

export const memberProjectionsQueryKey = (slug: string, member: string) =>
  [...MEMBER_PROJECTIONS_QUERY_PREFIX, slug, member] as const

/**
 * The query for one member's projection block, seeding the same store the roster
 * baseline seeds.
 *
 * The list response carries the `roster` view alone, because that is the only one
 * a list ROW paints. The drawer paints the activity timeline, the patrol state and
 * the driven-slot list, so without this read those blocks would have no baseline
 * until a live frame happened to arrive -- a member that has not moved since the
 * gateway started would show an empty drawer indefinitely.
 *
 * Seeding rather than returning the views to a caller keeps ONE source of truth on
 * the client: `useMemberProjection` already reads the store and already prefers a
 * live frame over a baseline through the store's higher-seq-wins rule, so a frame
 * that arrives while this request is in flight is not overwritten by it. The
 * consumers read the store; which request filled it is not theirs to know.
 *
 * A negative `asOfSeq` is NOT seeded from here. In the store that value is the
 * roster's refusal to attribute a slug, and it clears the slug's whole cache and
 * mutes the live frames that follow. This route never means that by it: a member
 * with no log yet answers an empty baseline at a real sequence, and every case the
 * roster refuses answers 409, so a negative here is only a read this route could
 * not stand behind. Muting a row that is displaying correctly is the wrong answer
 * to that, and attribution stays the roster list's to decide.
 *
 * Which is why the values go in through `apply` per key rather than `seed`. `seed`
 * LIFTS a refusal the roster has made -- that is how the mark is meant to clear,
 * on a roster baseline carrying a real sequence -- and this route has no standing
 * to do that. React Query re-runs a select over whatever block is cached, so a
 * block held from before a refusal would otherwise restore the pre-failure values
 * and re-admit live frames, with `data` still defined so no error state shows.
 * `apply` drops its input for a refused slug and never clears the mark, and it is
 * higher-seq-wins, so re-running it over the same block changes nothing. The empty
 * blocks this route can answer need none of `seed`'s truncation either: a logless
 * member's baseline is one the roster has already applied at the same sequence.
 */
export const memberProjectionsQuery = (slug: string, member: string) => ({
  queryKey: memberProjectionsQueryKey(slug, member),
  queryFn: async (): Promise<ProjectionsBlock> => await api.memberProjections(slug, member),
  staleTime: MEMBERS_ROSTER_STALE_MS,
  select: (block: ProjectionsBlock): ProjectionsBlock => {
    if (block.asOfSeq >= 0) {
      for (const [key, value] of Object.entries(block.values)) {
        memberProjectionStore.apply(slug, key, value, block.asOfSeq)
      }
    }
    return block
  },
})

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
