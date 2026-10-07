/**
 * Live sessions from every remote instance the sidebar can list, shaped as
 * ordinary Sessions-list rows, plus the per-crew groups the sidebar files them
 * under (`crewGroupsFor`).
 *
 * WHY GROUPED AND NOT MERGED: a session on a crew is OWNED by that crew, so
 * origin is a container, not a row property. The sidebar renders `Local` first
 * and then one collapsible group per crew, each headed by a status badge read
 * from the tunnel state. A local slot whose turns run on a crew
 * (`executor: 'remote'`) is filed in that crew's group too, because the crew is
 * where the conversation lives. With no crew group there is no group header at
 * all, so a single-machine sidebar is unchanged. Federated Older Sessions search
 * stays one rank-interleaved list: search is a query across machines.
 *
 * CACHED WHILE DISCONNECTED: every listable crew keeps its query observer, and
 * only the FETCH is gated on `connected`. A crew whose tunnel drops therefore
 * keeps its last answer in the query cache (memory only, so a reload clears it),
 * and the sidebar renders those rows dimmed. A crew that never answered has no
 * cache and contributes nothing.
 *
 * WHY `peer_id` AND NOT `instance_id`: a live `Slot` already declares
 * `instance_id`, and it means the OPPOSITE direction of travel — a LOCAL session
 * whose turns are DISPATCHED to a peer (`executor: 'remote'`). These rows are the
 * reverse: sessions the peer already OWNS, which this machine can only read.
 * Reusing the field would have collided in three ways. (1) It is a literal
 * duplicate declaration on one interface, so it does not compile. (2) Every
 * `slot.instance_id` predicate in ChatSidebar — roughly two dozen, covering
 * rename, drag, pin, folder placement, unread, the digit badge and the active
 * highlight — would start reading a remote-EXECUTED local slot as unreachable
 * and strip affordances that still work. (3) A peer's own slot can ITSELF be
 * `executor: 'remote'` bound to a THIRD machine, so overwriting `instance_id`
 * with the answering peer would destroy that binding rather than shadow it.
 * `isPeerRow()` in ChatSidebar is the single reader of these two fields.
 *
 * WHAT IS REACHABLE: the peer's live sessions, through the hub's own owner-only,
 * GET-only `/api/instances/{id}/chat-slots`. That route reads the peer's
 * `GET /api/chat/slots` and then FILTERS it, which is why it exists instead of this
 * hook calling the generic proxy: a local session bound to that peer for EXECUTION
 * (`executor: 'remote'`) is backed by a real slot ON the peer, so the peer lists it
 * alongside its own, and an unfiltered read renders that ONE conversation twice —
 * once as the local row the user can chat in, once as a read-only peer row. Only
 * the gateway can correlate the pair, because the binding's `remote_slot` is
 * deliberately never projected to the browser.
 * A remote instance's OLDER sessions stay out of reach: they live under the peer's
 * `/api/sessions`, which the proxy refuses — and the one prefix row that would
 * admit them would also admit `DELETE /api/sessions`, session-restart, a memory
 * read and a token-spending summarize. So this hook returns LIVE sessions and the
 * caller says so, rather than rendering rows it cannot fill.
 *
 * SORT KEY: the peer's `last_turn_ts` / `last_ts` / `created` ladder is forwarded
 * AS THE RAW ISO FIELDS and nothing is derived from it. `sessionOrder`'s
 * `slotActivityTs` already collapses exactly that ladder in exactly that
 * precedence, and it is what ranking, the date-segment header and the row label
 * each read for a live row — so a second copy here would be one definition of
 * "when did this last move" per file, which is how the two drift.
 *
 * An earlier revision also derived `modified` (epoch seconds) from the ladder, on
 * the belief that the header and label read `modified ?? created` and would
 * otherwise segment a row by its CREATION instant while ranking it by last
 * activity. That was wrong about this list: `modified ?? created` is the HISTORY
 * pane's rule, which a peer row never enters, and `lastActivityEpoch` falls back
 * to `slotActivityTs` when `modified` is absent. Dropping the field changes no
 * ordering and no header — the merge spec's recency-interleave and
 * one-header-per-bucket cases pass either way, which is what retired it.
 *
 * `last_message` is NOT a timestamp: it is an 80-char message PREVIEW string
 * (`slot_projection.py`: `redacted[:80]`). Assigning it to `modified` put a string
 * where a number belongs, made `tb - ta` NaN, and — because NaN makes every
 * comparison false — left the WHOLE merged list in arbitrary order rather than
 * merely misplacing remote rows. Forwarding only ISO fields is what makes that
 * class of mistake unavailable here.
 *
 * BLAST RADIUS: one query per instance, keyed per instance, `retry: false`. An
 * unreachable instance yields its own error and contributes no rows; it can never
 * empty or stall the local list, which is the objection that sank an earlier
 * fully-merged design. Callers surface `failed` plus `failure` through
 * `ErrorNotice` rather than faking rows for an instance that did not answer — a
 * read failure with nothing unsaved to lose, so the agent hand-off is on.
 */
import { useCallback, useMemo } from 'react'
import { useQueries, type UseQueryResult } from '@tanstack/react-query'
import { api, type InstanceView } from '../api/client'
import { hasDashboardPane } from '../utils/remoteCrew'

/** The fields this hook reads off a peer slot; everything else is ignored.
 *  Types mirror `slot_projection.py` — verified against the serializer, not
 *  assumed from the field names. */
interface PeerSlot {
  key: string
  title?: string
  running?: boolean
  pending_approval?: boolean
  /** ISO-8601. Moves only when a turn starts or ends — the ranking/display rung. */
  last_turn_ts?: string
  /** ISO-8601 of the newest saved row of any role; advances on every streamed tool call. */
  last_ts?: string
  /** ISO-8601 slot creation instant; last rung of the ladder. */
  created?: string
  agent?: string
  /** Stamped by `api_instances_chat_slots`, not by the peer: the hub resolves
   *  `<instance_id>:<key>` for every shaped row so the browser never composes
   *  that format itself. Optional only because the field is read defensively. */
  row_identity?: string
  /** The session that OPENED this one, both halves in the PEER's key space and
   *  absent when the peer recorded no creator. `key` is what the conductor lane
   *  NESTS on, resolved against rows of the same `peer_id` only, so it never
   *  nests under a local session whose key merely matches. `slot` is the child's
   *  own record of who opened it -- the "opened by" glyph on a row placed under
   *  nothing, and the move-detection baseline; a peer whose creator is gone
   *  sends `slot` with no `key`, the orphan case. */
  parent?: { slot?: string; key?: string }
  /** Present and true while the peer's lineage projection is still seeding, so
   *  this frame's `parent` is provisional. Absent on a settled frame. */
  lineage_pending?: boolean
}

/** A peer slot flattened into the shape the Sessions list already renders.
 *  Satisfies `ChatSlot`'s required trio (`key`, `messages`, `running`) so a remote
 *  row can be merged into the LIVE sessions list, not just the history drawer:
 *  these are the peer's OPEN sessions, and filing live sessions under "Older
 *  Sessions" (whose empty state reads "closed tabs appear here") was a category
 *  error.
 *
 *  `messages: 0` is honest rather than a placeholder — the slots list carries no
 *  message count, and the sidebar only uses it for a badge that should stay dark
 *  for a session whose transcript lives on another machine. */
export interface InstanceSessionRow {
  key: string
  title?: string
  /** The ISO ladder, forwarded raw. Deliberately NO `modified`: `slotActivityTs`
   *  collapses these three for ranking, the date-segment header and the row label
   *  alike, so a derived epoch field here would only be a second definition of the
   *  same ladder — see the SORT KEY note in the module docstring. */
  last_turn_ts?: string
  last_ts?: string
  created?: string
  agent?: string
  running: boolean
  messages: number
  pending_approval?: boolean
  /** The peer that OWNS this session — what makes the badge and remote activation
   *  fire, and the only thing `isPeerRow()` reads. Required here (not optional as
   *  on `Slot`) because a row this hook emits always came from some peer.
   *  Deliberately NOT `instance_id`; see the direction-of-travel note in the
   *  module docstring. */
  peer_id: string
  peer_name: string
  /** The identity the SERVER resolved for this row (`<instance_id>:<peer_key>`),
   *  forwarded verbatim. The sidebar keys rows on it. Composed in exactly one
   *  place — `api_instances_chat_slots` — so this format is not a contract the
   *  browser also has to know. */
  row_identity?: string
  /** The creator citation, forwarded in the shape a local `Slot` carries so the
   *  conductor lane reads a peer row exactly as it reads a local one. Each half
   *  is kept only when it is a string, and the object only when at least one
   *  half survived. `key` is resolved within this row's `peer_id`, never across
   *  origins (`lineage` in pages/chat-sidebar/conductor.ts); `slot` feeds
   *  `orphanCitation`, `citesParent` and the `citedCreatorRef` move baseline. */
  parent?: { slot?: string; key?: string }
  lineage_pending?: boolean
}

export interface InstanceSessions {
  rows: InstanceSessionRow[]
  /** Instances that are connected but did not answer, by display name. */
  failed: string[]
  /**
   * The FIRST failing read's own error text, kept beside the names in `failed`.
   *
   * The names alone say WHICH crew went quiet; this says why, and it is the
   * value `ErrorNotice` matches against the error journal to recover the
   * endpoint, HTTP status and backend `code`. Discarding it flattens a
   * diagnosable `peer_slots_malformed` 502 — or an expired-token 403 the user
   * can actually fix — into an unactionable "unavailable".
   *
   * FIRST rather than all of them: every failing crew's read went through the
   * same hub route, so a second message is nearly always the first one again,
   * and a banner that grows a line per crew buries the one line that matters.
   */
  failure?: string
  /** True while any instance's first fetch is outstanding. */
  loading: boolean
}

const REFRESH_MS = 15_000
const EMPTY: InstanceSessions = { rows: [], failed: [], loading: false }

/** Return `v` only when it really is a string, else `undefined`.
 *
 *  `PeerSlot`'s declared field types are a COMPILE-TIME claim about a payload that
 *  crossed a machine boundary, so nothing has checked them at runtime. A peer on a
 *  different version — or a hostile one — can answer `{"key":"x","title":{}}`, and
 *  an object reaching a row is rendered as a React child, which throws
 *  ("Objects are not valid as a React child") and takes the whole sidebar down
 *  with it. Dropping a malformed value is safe precisely because every field this
 *  guards is already optional, so each consumer handles its absence today. This
 *  extends the existing `typeof s.key !== 'string'` check to the rest of the
 *  projection rather than adding a new kind of validation.
 *
 *  It is also what keeps the ISO ladder's FALL-THROUGH honest, which is why each
 *  rung is guarded separately rather than the collapsed result being checked once.
 *  `slotActivityTs` picks a rung with `||`, so a truthy non-string
 *  (`last_turn_ts: {}`) would win that chain and then parse to NaN — discarding a
 *  perfectly good `last_ts` sitting behind it. Dropping the malformed rung here
 *  means the chain never sees it and the next VALID rung wins. */
const str = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined)

const isConnected = (inst: InstanceView): boolean => inst.status?.state === 'connected'

/** Crews whose chat slots can be listed: connected, and running a dashboard
 *  behind the forward. A fargate crew is connected without one, so asking it
 *  for slots would only ever report it as unreachable. */
function listsSessions(inst: InstanceView): boolean {
  return isConnected(inst) && hasDashboardPane(inst)
}

/** The badge a crew group header shows, from `TunnelStatus.state`. There is
 *  no "needs auth" badge: an auth failure diagnoses the same as an unreachable
 *  host today. `disconnected` and `stopped` both read as offline. */
export type CrewBadge = 'online' | 'reconnecting' | 'error' | 'offline'

export function crewBadge(state: string | undefined): CrewBadge | null {
  if (state === 'connected') return 'online'
  if (state === 'connecting') return 'reconnecting'
  if (state === 'error') return 'error'
  if (state === 'disconnected' || state === 'stopped') return 'offline'
  return null
}

/** One crew group in the sidebar. `offline` is true whenever the tunnel is not
 *  `connected`: its rows are the last cached answer, rendered dimmed. */
export interface CrewGroup {
  id: string
  name: string
  badge: CrewBadge | null
  offline: boolean
  /** `TunnelStatus.error`, shown as the badge's tooltip. */
  error?: string
}

/** The crew a sidebar row belongs to: the peer that owns it, or the crew a
 *  local slot's turns run on. `undefined` for an ordinary local row. */
export function crewOf(row: { peer_id?: string; executor?: string; instance_id?: string }): string | undefined {
  if (row.peer_id) return row.peer_id
  return row.executor === 'remote' && row.instance_id ? row.instance_id : undefined
}

/** The crew groups to render, in the instance list's order. A crew gets a group
 *  when it can be listed right now, or when some row belongs to it (cached peer
 *  rows, or a local slot running there). Crews with neither get no group, so
 *  with nothing connected and nothing bound the sidebar has no groups at all. */
export function crewGroupsFor(
  instances: readonly InstanceView[],
  rows: readonly { peer_id?: string; executor?: string; instance_id?: string }[],
): CrewGroup[] {
  const owned = new Set<string>()
  for (const r of rows) {
    const id = crewOf(r)
    if (id) owned.add(id)
  }
  return instances
    .filter(inst => listsSessions(inst) || owned.has(inst.id))
    .map(inst => ({
      id: inst.id,
      name: inst.name || inst.id,
      badge: crewBadge(inst.status?.state),
      offline: !isConnected(inst),
      ...(inst.status?.error ? { error: inst.status.error } : {}),
    }))
}

/**
 * @param enabled the preview flag. When false this issues NO request at all —
 *   not a request whose rows are discarded. The flag gates the wire, because this
 *   hook runs inside a sidebar every dashboard user mounts.
 * @param instances the caller's OWN `['instances']` result. Deliberately a
 *   parameter rather than a second `useQuery` here: the sidebar already holds this
 *   list, and a duplicate cache observer notified on the same key re-rendered the
 *   whole sidebar — one such spurious render landing mid-rename blurs the rename
 *   textarea and cancels the edit (`ChatSidebarRenameFocus.integration.test.tsx`).
 *   One observer, owned by the caller that also gates it, removes that class of
 *   render coupling instead of suppressing its symptom.
 * @param instancesUnanswered whether that list has yet to arrive, so the caller
 *   can say "checking" rather than implying an empty peer set. The caller must
 *   derive this WITHOUT touching react-query's `isLoading` / `isFetching`:
 *   those are tracked properties, and subscribing a sidebar to fetch-status
 *   churn re-renders it on every background refetch — one of which landing
 *   mid-rename cancels the edit.
 */
export function useInstanceSessions(
  enabled: boolean,
  instances: readonly InstanceView[] = [],
  instancesUnanswered = false,
): InstanceSessions {
  // Every crew that runs a dashboard, connected or not: the observer is what keeps
  // a disconnected crew's last answer in the cache. Only connected ones FETCH.
  const listed = useMemo(
    () => (enabled ? instances.filter(hasDashboardPane) : []),
    [enabled, instances],
  )

  const combineResults = useCallback((results: UseQueryResult<PeerSlot[]>[]): InstanceSessions => {
    const rows: InstanceSessionRow[] = []
    const failed: string[] = []
    let failure: string | undefined
    let loading = false

    results.forEach((r, i) => {
      const inst = listed[i]
      if (!inst) return
      const name = inst.name || inst.id
      // A disconnected crew contributes only its cached rows: its last error and
      // its never-started first fetch say nothing about the crew right now.
      const live = isConnected(inst)
      if (live && r.isError) {
        failed.push(name)
        // `??=` so the FIRST failure wins, matching `failed[0]`. Guarded on
        // `Error` rather than cast: react-query types `error` as `Error | null`,
        // but a rejection can carry any value and a non-Error reaching an error
        // banner as `[object Object]` is worse than saying only which crew went
        // quiet.
        if (r.error instanceof Error && r.error.message) failure ??= r.error.message
        return
      }
      if (live && r.isLoading) { loading = true; return }
      if (!Array.isArray(r.data)) return
      for (const s of r.data) {
        if (!s || typeof s.key !== 'string') continue
        // Same runtime guard as `str`, one level down: `parent` crossed a machine
        // boundary too, and the lane dereferences both halves on every frame.
        const cited = s.parent && typeof s.parent === 'object' ? s.parent : undefined
        const parentKey = cited ? str(cited.key) : undefined
        const parentSlot = cited ? str(cited.slot) : undefined
        const parent = parentKey || parentSlot
          ? {
            ...(parentSlot ? { slot: parentSlot } : {}),
            ...(parentKey ? { key: parentKey } : {}),
          }
          : undefined
        rows.push({
          ...(parent ? { parent } : {}),
          ...(s.lineage_pending === true ? { lineage_pending: true } : {}),
          key: s.key,
          title: str(s.title),
          last_turn_ts: str(s.last_turn_ts),
          last_ts: str(s.last_ts),
          created: str(s.created),
          agent: str(s.agent),
          // An offline crew's row is a cached answer: its last `running` and
          // `pending_approval` are not true now, so they are dropped.
          running: live && s.running === true,
          messages: 0,
          // Normalized like `running`: a truthy non-boolean from a peer would
          // otherwise raise a pending-approval badge the peer never claimed.
          pending_approval: live && s.pending_approval === true,
          peer_id: inst.id,
          peer_name: name,
          row_identity: str(s.row_identity),
        })
      }
    })

    return { rows, failed, failure, loading }
  }, [listed])

  // `combine` structurally shares its result while the underlying query results
  // are unchanged. Without it, useQueries returns a fresh array on every render,
  // which rebuilt `rows` and forced the entire Sessions list to filter and sort
  // again after unrelated sidebar state changes.
  const combined = useQueries({
    queries: listed.map(inst => ({
      queryKey: ['instance-slots', inst.id],
      queryFn: () => api.instanceChatSlots(inst.id) as Promise<PeerSlot[]>,
      enabled: enabled && isConnected(inst),
      refetchInterval: REFRESH_MS,
      retry: false,
    })),
    combine: combineResults,
  })

  return useMemo(() => {
    if (!enabled) return EMPTY
    return instancesUnanswered && !combined.loading
      ? { ...combined, loading: true }
      : combined
  }, [enabled, combined, instancesUnanswered])
}
