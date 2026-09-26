/**
 * One crewmate's Perpetual mode, read for the two places its switch sits: the
 * detail page (the crew editor's Schedules pane) and the Crewmates page side
 * panel.
 *
 * Perpetual mode is the crewmate's restart policy: ON, it keeps waking on its
 * own thread with no cycle or time cap; OFF, it works only when asked. The
 * reading here is assembled from two queries the dashboard already keeps live:
 *
 * - the roster (`GET /api/members`) names the crewmate's slug, the slot key its
 *   own thread is bound to, and its `perpetual` reading (`on` / `off` /
 *   `none`) -- the editor knows the crew by NAME only, and a slug is lossy (two
 *   crews can share one), so the row is matched on the exact name and nothing
 *   is re-derived here. The STATE is the roster's word: the backend computes it
 *   from the same registry with `is_structured_monitor_loop` applied, so a
 *   structured monitor on the crewmate's thread -- which `/api/autonudge`
 *   publishes as a reduced row with no cycle accounting -- reads `none`, never
 *   ON, and the switch never offers a change the route would refuse (409);
 * - the loop registry (`GET /api/autonudge`), filtered by that slot key, for
 *   the READOUTS an ON or OFF state carries (interval, wakes, last / next
 *   fire, the stop reason and words). The websocket hook invalidates
 *   `AUTONUDGE_LOOPS_QUERY_KEY` on every `autonudge_state` frame and on
 *   reconnect, so a stop or a wake re-renders without this hook listening for
 *   anything; the interval is a floor under that for a dropped frame. The
 *   roster is re-read when the registry changes for the same reason.
 *
 * A roster row from a server that predates the `perpetual` field falls back to
 * the record itself (active = on, present = off, absent = none). `failed` is
 * kept distinct from "no loop" so a read that failed is never rendered as the
 * affirmative "never turned on".
 */
import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY, membersRosterQuery } from '../../api/membersQuery'
import { AUTONUDGE_LOOPS_QUERY_KEY, type AutoNudgeLoop } from '../autoNudgeLoop'
import { PERPETUAL_DEFAULT_BANNER, PERPETUAL_DEFAULT_INSTRUCTION, PERPETUAL_FEATURE_NAME } from './perpetualBrief.prompt'

export { PERPETUAL_DEFAULT_BANNER, PERPETUAL_DEFAULT_INSTRUCTION }

/** How often the registry is re-read while nobody pushes a frame. Coarse: the
 *  frames carry the changes; this only catches a frame lost to a dropped socket. */
const PERPETUAL_REFRESH_MS = 30_000

export type CrewPerpetualState = 'on' | 'off' | 'none'

/** Is this line the switch's own default brief (see perpetualBrief.prompt.ts)
 *  or the bare feature name? Both hosts show a loop's instruction line; for a
 *  loop the SWITCH armed that line only restates the feature the block is
 *  already titled with, so the hosts hide it. A crewmate that rewrote its own
 *  brief (`monitor_update`) keeps its line: only these exact strings are the
 *  default. */
export function isDefaultPerpetualBrief(text: string | undefined): boolean {
  const s = (text ?? '').trim()
  return (
    s === '' || s === PERPETUAL_DEFAULT_BANNER || s === PERPETUAL_DEFAULT_INSTRUCTION || s === PERPETUAL_FEATURE_NAME
  )
}

/** The instruction line a host shows for a loop, or '' when every candidate
 *  is the default: the banner when it says something of its own, else the
 *  message when THAT does (a member that changed its brief but kept the
 *  banner), else nothing. */
export function perpetualBriefText(loop: { banner?: string; message?: string }): string {
  if (!isDefaultPerpetualBrief(loop.banner)) return loop.banner ?? ''
  if (!isDefaultPerpetualBrief(loop.message)) return loop.message ?? ''
  return ''
}

export interface CrewPerpetualReading {
  /** The crewmate's slug, from its roster row; '' until the roster answers. */
  slug: string
  /** The slot key of its own thread; '' when the thread was never opened. */
  slotKey: string
  /** The loop record on that thread, when the registry holds one. */
  loop: AutoNudgeLoop | undefined
  /** ON = the loop is active; OFF = a record exists and is paused; none = no record. */
  state: CrewPerpetualState
  /** Both reads have answered (with data or with an error). */
  loaded: boolean
  /** A read failed and nothing is known -- distinct from `state === 'none'`. */
  failed: boolean
  /** The roster answered and holds no row for this crew (renamed away, or
   *  not a global crew). The switch has nothing to address then. */
  missing: boolean
  /** The registry holds a record on the thread the roster does NOT count as
   *  the switch's loop -- a structured monitor (`monitor_watch`), published
   *  as a reduced row. Perpetual mode is then not in use because of it, which
   *  is a different sentence from "never turned on". */
  monitor: boolean
  /** The gateway runs no nudge service (`GET /api/autonudge` answers
   *  `enabled: false`): Perpetual mode cannot run here, whatever the roster
   *  says. `true` until the registry has answered, so a loading card never
   *  claims the mode is unavailable. */
  enabled: boolean
}

export interface CrewPerpetualOptions {
  /** Keep the registry's floor refetch running while a crew is on screen (the
   *  editor's default). The Crewmates page passes `false`: its block already
   *  re-renders from the pushed `wake` projection, so a second poll there
   *  would only spend requests. Registry changes still refresh the roster. */
  poll?: boolean
}

export function useCrewPerpetual(crew: string, { poll = true }: CrewPerpetualOptions = {}): CrewPerpetualReading {
  const queryClient = useQueryClient()
  const roster = useQuery(membersRosterQuery)
  // The page hosts this hook with no crew open too (``editing`` empty): the
  // floor refetch and the roster re-read below run only while a crew's
  // reading is on screen, so a closed editor costs no polling.
  const watching = !!crew
  const loops = useQuery({
    queryKey: AUTONUDGE_LOOPS_QUERY_KEY,
    queryFn: () => api.autonudgeList(),
    refetchInterval: watching && poll ? PERPETUAL_REFRESH_MS : false,
    refetchOnReconnect: true,
  })
  // The state is the roster's word, but the roster is a registry projection
  // that no frame pushes: when the registry answers anew (a frame-driven
  // invalidation or the optional floor refetch), the roster is re-read too,
  // so a stop the crewmate made itself moves either switch within the same
  // tick. `poll` controls only the floor refetch; it cannot suppress this
  // projection refresh on the Crewmates page.
  const loopsUpdatedAt = loops.dataUpdatedAt
  useEffect(() => {
    if (!watching || !loopsUpdatedAt) return
    void queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
  }, [watching, loopsUpdatedAt, queryClient])
  const row = roster.data?.find((r) => r.name === crew)
  const slotKey = row?.slot_key ?? ''
  const record = slotKey ? loops.data?.loops.find((lp) => lp?.slot_key === slotKey) : undefined
  const state: CrewPerpetualState =
    row?.perpetual === 'on' || row?.perpetual === 'off' || row?.perpetual === 'none'
      ? row.perpetual
      : record?.active ? 'on' : record ? 'off' : 'none'
  // A record the roster does not count as the switch's loop (a structured
  // monitor's reduced row) carries no readouts worth showing under the switch.
  const loop = state === 'none' ? undefined : record
  const rosterLoaded = roster.data !== undefined || roster.isError
  const loopsLoaded = loops.data !== undefined || loops.isError
  // A refetch error after a good read keeps the last data: only a read that
  // never answered is a failure the block has to admit.
  const failed = (roster.data === undefined && roster.isError) || (loops.data === undefined && loops.isError)
  return {
    slug: row?.slug ?? '',
    slotKey,
    loop,
    state,
    loaded: rosterLoaded && loopsLoaded,
    failed,
    missing: roster.data !== undefined && !row,
    monitor: state === 'none' && !!record,
    enabled: loops.data === undefined || loops.data.enabled !== false,
  }
}
