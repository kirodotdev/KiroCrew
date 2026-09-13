/**
 * Live sessions from every CONNECTED remote instance, shaped as ordinary
 * Sessions-list rows so they can be MERGED into the local list rather than
 * grouped into a region of their own.
 *
 * WHY MERGED AND NOT SECTIONED: the sidebar already treats origin as a PROPERTY
 * OF A ROW rather than a bucket a row lives in — federated search stamps the
 * peer that answered onto each history row, and the live list already renders a
 * per-row instance badge and a remote activation path. So the honest shape for
 * "see every session together" is one recency-ordered list with the badge doing
 * the distinguishing. A per-instance section would have added a second grammar
 * for something the row model already expresses.
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
  /** ISO-8601 of the newest row of any role; advances on every streamed tool call. */
  last_ts?: string
  /** ISO-8601 slot creation instant; last rung of the ladder. */
  created?: string
  agent?: string
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

function isConnected(inst: InstanceView): boolean {
  return inst.status?.state === 'connected'
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
  const connected = useMemo(
    () => (enabled ? instances.filter(isConnected) : []),
    [enabled, instances],
  )

  const combineResults = useCallback((results: UseQueryResult<PeerSlot[]>[]): InstanceSessions => {
    const rows: InstanceSessionRow[] = []
    const failed: string[] = []
    let failure: string | undefined
    let loading = false

    results.forEach((r, i) => {
      const inst = connected[i]
      if (!inst) return
      const name = inst.name || inst.id
      if (r.isError) {
        failed.push(name)
        // `??=` so the FIRST failure wins, matching `failed[0]`. Guarded on
        // `Error` rather than cast: react-query types `error` as `Error | null`,
        // but a rejection can carry any value and a non-Error reaching an error
        // banner as `[object Object]` is worse than saying only which crew went
        // quiet.
        if (r.error instanceof Error && r.error.message) failure ??= r.error.message
        return
      }
      if (r.isLoading) { loading = true; return }
      if (!Array.isArray(r.data)) return
      for (const s of r.data) {
        if (!s || typeof s.key !== 'string') continue
        rows.push({
          key: s.key,
          title: str(s.title),
          last_turn_ts: str(s.last_turn_ts),
          last_ts: str(s.last_ts),
          created: str(s.created),
          agent: str(s.agent),
          running: s.running === true,
          messages: 0,
          // Normalized like `running`: a truthy non-boolean from a peer would
          // otherwise raise a pending-approval badge the peer never claimed.
          pending_approval: s.pending_approval === true,
          peer_id: inst.id,
          peer_name: name,
        })
      }
    })

    return { rows, failed, failure, loading }
  }, [connected])

  // `combine` structurally shares its result while the underlying query results
  // are unchanged. Without it, useQueries returns a fresh array on every render,
  // which rebuilt `rows` and forced the entire Sessions list to filter and sort
  // again after unrelated sidebar state changes.
  const combined = useQueries({
    queries: connected.map(inst => ({
      queryKey: ['instance-slots', inst.id],
      queryFn: () => api.instanceChatSlots(inst.id) as Promise<PeerSlot[]>,
      enabled,
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
