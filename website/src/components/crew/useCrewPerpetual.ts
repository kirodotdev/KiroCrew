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
import { useEffect } from "react";
import { hashKey, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import {
  MEMBERS_ROSTER_QUERY_KEY,
  membersRosterQuery,
} from "../../api/membersQuery";
import {
  AUTONUDGE_LOOPS_QUERY_KEY,
  type AutoNudgeLoop,
} from "../autoNudgeLoop";
/** How often the registry is re-read while nobody pushes a frame. Coarse: the
 *  frames carry the changes; this only catches a frame lost to a dropped socket. */
const PERPETUAL_REFRESH_MS = 30_000;

/**
 * The interval a crewmate's loop is armed with when the owner switches the mode
 * on and it has no loop of its own yet, mirroring `PERPETUAL_DEFAULT_IDLE_SECS`
 * in the members handler. The owner never sets an interval; the crewmate
 * retunes its own from inside a wake (`monitor_update`).
 *
 * Read ONLY to state the first-wake timing and the per-day estimate BEFORE a
 * record exists -- once one does, its `idle_secs` is the truth. If the backend
 * default changes, this estimate can drift, so the UI labels it as an estimate.
 */
export const PERPETUAL_DEFAULT_IDLE_SECS = 3600;

const SECONDS_PER_DAY = 86_400;

/** The interval a reading should be stated with: the record's own when it has
 *  one, else the interval an arm would start on. */
export function perpetualIdleSecs(
  loop?: { idle_secs?: number } | null,
): number {
  const secs = loop?.idle_secs;
  return typeof secs === "number" && Number.isFinite(secs) && secs > 0
    ? secs
    : PERPETUAL_DEFAULT_IDLE_SECS;
}

/**
 * Wakes a day at `idleSecs`, for the cost the switch states before it is pressed
 * ("one model turn per wake, about N a day"). An ESTIMATE by construction: the
 * loop is deadline-preserving and each interval starts when the current wake
 * ENDS, so a wake that runs long pushes the next one out and the real count can
 * only be lower than this.
 *
 * Coarse above ten a day (a whole number: "about 288" carries no less than
 * "287.6"), one decimal below it, so an interval near or past a day does not
 * collapse to "0" and claim the crewmate never wakes. A missing or nonsense
 * interval reads as the default arm's, never as zero.
 */
export function wakesPerDay(idleSecs: number): number {
  const secs =
    Number.isFinite(idleSecs) && idleSecs > 0
      ? idleSecs
      : PERPETUAL_DEFAULT_IDLE_SECS;
  const perDay = SECONDS_PER_DAY / secs;
  return perDay >= 10 ? Math.round(perDay) : Math.round(perDay * 10) / 10;
}

export type CrewPerpetualState = "on" | "off" | "none";

/** The instruction line a host shows for a loop. Prefer its short banner;
 * otherwise show the full message. The backend owns both strings, so the UI
 * never copies them merely to decide whether they are worth showing. */
export function perpetualBriefText(loop: {
  banner?: string;
  message?: string;
}): string {
  return loop.banner?.trim() ? loop.banner : (loop.message ?? "");
}

export interface CrewPerpetualReading {
  /** The crewmate's slug, from its roster row; '' until the roster answers. */
  slug: string;
  /** The slot key of its own thread; '' when the thread was never opened. */
  slotKey: string;
  /** Safe route to review the chat whose goals and instructions wakes continue. */
  chatHref: string;
  /** The loop record on that thread, when the registry holds one. */
  loop: AutoNudgeLoop | undefined;
  /** ON = the loop is active; OFF = a record exists and is paused; none = no record. */
  state: CrewPerpetualState;
  /** Both reads have answered (with data or with an error). */
  loaded: boolean;
  /** A read failed and nothing is known -- distinct from `state === 'none'`. */
  failed: boolean;
  /** The roster answered and holds no row for this crew (renamed away, or
   *  not a global crew). The switch has nothing to address then. */
  missing: boolean;
  /** The registry holds a record on the thread the roster does NOT count as
   *  the switch's loop -- a structured monitor (`monitor_watch`), published
   *  as a reduced row. Perpetual mode is then not in use because of it, which
   *  is a different sentence from "never turned on". */
  monitor: boolean;
  /** The gateway runs no nudge service (`GET /api/autonudge` answers
   *  `enabled: false`): Perpetual mode cannot run here, whatever the roster
   *  says. `true` until the registry has answered, so a loading card never
   *  claims the mode is unavailable. */
  enabled: boolean;
  /** Re-ask BOTH reads. The host's `failed` notice offers it: a read that
   *  never answered is usually transient, and the reading is assembled from
   *  two queries, so retrying only the one that failed would leave the other
   *  on its stale answer. */
  retry: () => void;
}

export interface CrewPerpetualOptions {
  /** Keep the registry's floor refetch running while a crew is on screen (the
   *  editor's default). The Crewmates page passes `false`: its block already
   *  re-renders from the pushed `wake` projection, so a second poll there
   *  would only spend requests. Registry changes still refresh the roster. */
  poll?: boolean;
}

export function useCrewPerpetual(
  crew: string,
  { poll = true }: CrewPerpetualOptions = {},
): CrewPerpetualReading {
  const queryClient = useQueryClient();
  const roster = useQuery(membersRosterQuery);
  // The page hosts this hook with no crew open too (``editing`` empty): the
  // floor refetch and the roster re-read below run only while a crew's
  // reading is on screen, so a closed editor costs no polling.
  const watching = !!crew;
  const loops = useQuery({
    queryKey: AUTONUDGE_LOOPS_QUERY_KEY,
    queryFn: () => api.autonudgeList(),
    refetchInterval: watching && poll ? PERPETUAL_REFRESH_MS : false,
    staleTime: PERPETUAL_REFRESH_MS,
    refetchOnReconnect: true,
  });
  // The state is the roster's word, but the roster is a registry projection
  // that no frame pushes: when the registry answers anew (a frame-driven
  // invalidation or the optional floor refetch), the roster is re-read too,
  // so a stop the crewmate made itself moves either switch within the same
  // tick. `poll` controls only the floor refetch; it cannot suppress this
  // projection refresh on the Crewmates page.
  useEffect(() => {
    if (!watching) return;
    const loopsQueryHash = hashKey(AUTONUDGE_LOOPS_QUERY_KEY);
    let skipInitialSuccess =
      queryClient.getQueryState(AUTONUDGE_LOOPS_QUERY_KEY)?.fetchStatus ===
      "fetching";
    return queryClient.getQueryCache().subscribe((event) => {
      if (
        event.type !== "updated" ||
        event.action.type !== "success" ||
        event.query.queryHash !== loopsQueryHash
      ) {
        return;
      }
      if (skipInitialSuccess) {
        skipInitialSuccess = false;
        return;
      }
      void queryClient.invalidateQueries({
        queryKey: MEMBERS_ROSTER_QUERY_KEY,
      });
    });
  }, [watching, queryClient]);
  const row = roster.data?.find((r) => r.name === crew);
  const slotKey = row?.slot_key ?? "";
  const record = slotKey
    ? loops.data?.loops.find((lp) => lp?.slot_key === slotKey)
    : undefined;
  const state: CrewPerpetualState =
    row?.perpetual === "on" ||
    row?.perpetual === "off" ||
    row?.perpetual === "none"
      ? row.perpetual
      : record?.active
        ? "on"
        : record
          ? "off"
          : "none";
  // A record the roster does not count as the switch's loop (a structured
  // monitor's reduced row) carries no readouts worth showing under the switch.
  // Its positive marker prevents full self-arm and capped rows that also read
  // `none` from inheriting the structured monitor's stop advice.
  const loop = record?.record_kind === "structured_monitor" ? undefined : record;
  const rosterLoaded = roster.data !== undefined || roster.isError;
  const loopsLoaded = loops.data !== undefined || loops.isError;
  // A refetch error after a good read keeps the last data: only a read that
  // never answered is a failure the block has to admit.
  const failed =
    (roster.data === undefined && roster.isError) ||
    (loops.data === undefined && loops.isError);
  return {
    slug: row?.slug ?? "",
    slotKey,
    chatHref: crew ? `/members?member=${encodeURIComponent(crew)}` : "",
    loop,
    state,
    loaded: rosterLoaded && loopsLoaded,
    failed,
    missing: roster.data !== undefined && !row,
    monitor: state === "none" && record?.record_kind === "structured_monitor",
    enabled: loops.data === undefined || loops.data.enabled !== false,
    retry: () => {
      void roster.refetch();
      void loops.refetch();
    },
  };
}
