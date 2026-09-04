/**
 * Live sessions from every CONNECTED remote instance, shaped as ordinary
 * Sessions-list rows so they can be MERGED into the local list rather than
 * grouped into a region of their own.
 *
 * WHY MERGED AND NOT SECTIONED: the sidebar already carries `instance_id` /
 * `instance_name` on a row (federated search populates them) and already renders
 * an instance badge and a remote activation path. Origin is therefore already a
 * PROPERTY OF A ROW in this component, not a bucket a row lives in — so the
 * honest shape for "see every session together" is one recency-ordered list with
 * the badge doing the distinguishing. A per-instance section would have added a
 * second grammar for something the row model already expresses.
 *
 * WHAT IS REACHABLE: the peer's `GET /api/chat/slots`, which the instance proxy's
 * allowlist already admits (`("api","chat")` covers the whole subtree). A remote
 * instance's OLDER sessions live under the peer's `/api/sessions`, which the proxy
 * refuses — and the one prefix row that would admit them would also admit
 * `DELETE /api/sessions`, session-restart, a memory read and a token-spending
 * summarize. So this hook returns LIVE sessions and the caller says so, rather
 * than rendering rows it cannot fill.
 *
 * SORT KEY: collapse the peer's `last_turn_ts` / `last_ts` / `created` ladder into
 * `modified` (epoch SECONDS) via `ladderEpoch`, and keep the raw ISO fields too.
 * `modified` is what this list ranks, segments and labels on, so deriving it once
 * is what stops those three from disagreeing — see `ladderEpoch` for why leaving
 * it absent produced duplicate date headers. `last_message` is NOT a timestamp: it
 * is an 80-char message PREVIEW string (`slot_projection.py`: `redacted[:80]`).
 * Assigning it to `modified` put a string where a number belongs, made `tb - ta`
 * NaN, and — because NaN makes every comparison false — left the WHOLE merged list
 * in arbitrary order rather than merely misplacing remote rows.
 *
 * BLAST RADIUS: one query per instance, keyed per instance, `retry: false`. An
 * unreachable instance yields its own error and contributes no rows; it can never
 * empty or stall the local list, which is the objection that sank an earlier
 * fully-merged design. Callers surface `failed` as one line rather than faking
 * rows for an instance that did not answer.
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
  /** Epoch SECONDS derived from the ISO ladder — see `ladderEpoch`. Ranking, the
   *  date-segment header and the row label all read this, so they cannot disagree. */
  modified?: number
  last_turn_ts?: string
  last_ts?: string
  created?: string
  agent?: string
  running: boolean
  messages: number
  pending_approval?: boolean
  /** Present on remote rows only — what makes the badge and remote activation fire. */
  instance_id: string
  instance_name: string
}

export interface InstanceSessions {
  rows: InstanceSessionRow[]
  /** Instances that are connected but did not answer, by display name. */
  failed: string[]
  /** True while any instance's first fetch is outstanding. */
  loading: boolean
}

const REFRESH_MS = 15_000
const EMPTY: InstanceSessions = { rows: [], failed: [], loading: false }

/**
 * The peer's ISO ladder collapsed to the epoch SECONDS this list ranks on.
 *
 * WHY `modified` IS POPULATED RATHER THAN LEFT ABSENT: three consumers must agree
 * on one value or the list visibly contradicts itself. `lastActivityEpoch` ranks
 * on `modified` (short-circuiting before the ISO ladder), while the date-segment
 * header and the row's own label read `modified ?? created`. Passing the ladder
 * through but leaving `modified` unset makes a row SORT by last activity and get
 * SEGMENTED by its creation instant — so the segment flips back and forth down
 * the list and emits a duplicate `YESTERDAY` / `LAST 7 DAYS` header at every flip.
 * Collapsing the ladder here once gives all three the same number.
 *
 * `last_turn_ts` first, matching `slotActivityTs`: it moves only when a turn
 * starts or ends, whereas `last_ts` advances on every streamed tool call and
 * would make the list churn while an agent works.
 *
 * Returns undefined for an unparseable or absent instant, which ranks the row as
 * "no timestamp" instead of poisoning the comparator with NaN.
 */
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
 *  projection rather than adding a new kind of validation. */
const str = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined)

function ladderEpoch(slot: PeerSlot): number | undefined {
  // Each rung is validated separately so a malformed higher rung falls through to
  // the next VALID one. Reading the raw fields instead would let a truthy
  // non-string (`last_turn_ts: {}`) win the `||` chain and then parse to NaN,
  // discarding a perfectly good `last_ts` behind it.
  const iso = str(slot.last_turn_ts) || str(slot.last_ts) || str(slot.created)
  if (!iso) return undefined
  const ms = new Date(iso).getTime()
  return Number.isNaN(ms) ? undefined : ms / 1000
}

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
    let loading = false

    results.forEach((r, i) => {
      const inst = connected[i]
      if (!inst) return
      const name = inst.name || inst.id
      if (r.isError) { failed.push(name); return }
      if (r.isLoading) { loading = true; return }
      if (!Array.isArray(r.data)) return
      for (const s of r.data) {
        if (!s || typeof s.key !== 'string') continue
        rows.push({
          key: s.key,
          title: str(s.title),
          modified: ladderEpoch(s),
          last_turn_ts: str(s.last_turn_ts),
          last_ts: str(s.last_ts),
          created: str(s.created),
          agent: str(s.agent),
          running: s.running === true,
          messages: 0,
          // Normalized like `running`: a truthy non-boolean from a peer would
          // otherwise raise a pending-approval badge the peer never claimed.
          pending_approval: s.pending_approval === true,
          instance_id: inst.id,
          instance_name: name,
        })
      }
    })

    return { rows, failed, loading }
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
