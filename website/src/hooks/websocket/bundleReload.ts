/** The `dashboard` status frame, and reloading a tab whose bundle is stale
 *  after a gateway update.
 *
 *  A status frame is stored unless it proves the bundle stale. Two
 *  independent signals: the `dashboard` status frame's `version` and
 *  served-bundle hash moving between pushes, which reaches every open tab;
 *  and the per-tab restart latch an in-app update arms on its `restarting`
 *  step, which the next reconnect consumes, so the tab that watched the
 *  update reloads at once, before any status frame arrives. */
import { useMemo, useRef } from 'react'
import type { AppDispatch, RootState } from '../../store'
import { sseStatus, setUpdateProgress } from '../../store/dashboardSlice'
import type { StatusData } from '../../types'
import type { FrameData } from './frames'

/** sessionStorage key latched when an in-app update reaches its `restarting`
 *  step. The gateway then execs itself and the socket drops; on the next
 *  successful reconnect the latch tells this tab to reload — the signal the
 *  version comparison cannot give when a git checkout rebuilds the SAME
 *  version. Session-scoped on purpose: only the tab that watched the update
 *  restart needs it (other tabs recover via the bundle-id comparison). */
export const UPDATE_RESTART_LATCH_KEY = 'mc-update-restarting'

/** How long the latch stays honored. A restart resolves in seconds; a latch
 *  older than this belongs to an update the user cancelled or that died
 *  before exec, and reloading over an unrelated later reconnect would look
 *  like the app randomly refreshing itself. */
export const UPDATE_RESTART_LATCH_TTL_MS = 15 * 60 * 1000

/** Read-and-clear the restart latch. True only when a fresh latch existed —
 *  the caller reloads exactly once per latched update. Clears a stale latch
 *  too, so an abandoned update cannot linger into a later session. */
export function consumeUpdateRestartLatch(now: number = Date.now()): boolean {
  let raw: string | null = null
  try { raw = sessionStorage.getItem(UPDATE_RESTART_LATCH_KEY) } catch { return false }
  if (raw === null) return false
  try { sessionStorage.removeItem(UPDATE_RESTART_LATCH_KEY) } catch { /* best effort */ }
  const at = Number(raw)
  return Number.isFinite(at) && now - at < UPDATE_RESTART_LATCH_TTL_MS
}

/** An `update_progress` frame: arm or clear the latch, then show the step. */
export function handleUpdateProgress(dispatch: AppDispatch, data: FrameData): void {
  const prog = data as { step: string; detail: string }
  // `restarting` is the last event before the gateway execs itself
  // and this socket dies — latch it so the reconnect handler knows
  // the next successful connect is a post-update gateway and this
  // tab's bundle must be reloaded (a same-version rebuild moves
  // neither `version` nor anything else the tab compares).
  if (prog.step === 'restarting') {
    try { sessionStorage.setItem(UPDATE_RESTART_LATCH_KEY, String(Date.now())) } catch { /* best effort */ }
  } else if (prog.step === 'failed' || prog.step === 'error' || prog.step === 'done') {
    // A failure AFTER `restarting` was pushed (invalid exe path) means
    // no exec is coming — an armed latch would reload over the next
    // unrelated blip. `done` is only ever simulated, same cleanup.
    try { sessionStorage.removeItem(UPDATE_RESTART_LATCH_KEY) } catch { /* best effort */ }
  }
  if (prog.step === 'done') {
    dispatch(setUpdateProgress(null))
  } else {
    dispatch(setUpdateProgress(prog))
  }
}

/**
 * Status fields that move on every push while the dashboard is idle, and that
 * no dashboard surface reads from the store. The gateway sends them for the
 * CLI and other `/api/status` clients; Overview and Services derive uptime
 * from `start_time` with their own clock.
 */
const VOLATILE_STATUS_KEYS: ReadonlySet<string> = new Set([
  'uptime', 'uptime_secs', 'messages_received', 'cron', 'stats', 'stats_summary',
  'gateway_rss_mb', 'yolo_remaining_secs',
])

/** A comparable key for a frame's non-volatile fields. */
function stableStatusKey(data: StatusData): string {
  return JSON.stringify(data, (key, value) => (VOLATILE_STATUS_KEYS.has(key) ? undefined : value))
}

export interface BundleReload {
  /** A `dashboard` status frame: reload on a new version or bundle, else
   *  store the status. */
  onDashboardStatus(data: StatusData): void
}

export function useBundleReload(
  dispatch: AppDispatch,
  getDashboard?: () => Pick<RootState['dashboard'], 'status' | 'connected'>,
): BundleReload {
  // The last status frame's version and served-bundle hash. Kept across
  // reconnects on purpose, so a restarted gateway with a new version or
  // bundle reloads this tab on its first status frame. A same-version
  // rebuild (a git checkout's in-app update) moves the bundle id while
  // `version` stays put, so it is the cross-push comparison that catches the
  // case the version check cannot — and it reaches every open tab, not just
  // the one that clicked Update. '' from the server means "no built bundle /
  // unknown" and is never treated as a change in either direction.
  const lastVersionRef = useRef<string | null>(null)
  const lastBundleIdRef = useRef<string | null>(null)
  // The non-volatile key of the last frame this hook stored, and the status
  // object the store held right after it. A frame is skipped only while both
  // still describe the store: the socket is marked connected and no other
  // writer (an `/api/status` reply, a disconnect) has replaced the status.
  const lastStoredRef = useRef<{ key: string; status: StatusData | null } | null>(null)

  return useMemo<BundleReload>(() => ({
    onDashboardStatus(data) {
      // Detect server version change → full reload (actual update)
      const prev = lastVersionRef.current
      const next = data.version
      if (next) lastVersionRef.current = next
      // Same rule for the served-bundle hash: a git checkout's in-app
      // update rebuilds the SAME version, so `version` never moves and
      // only the bundle id records that this tab's JS is now stale.
      const prevBundle = lastBundleIdRef.current
      const nextBundle = data.bundle_id
      if (nextBundle) lastBundleIdRef.current = nextBundle
      if ((prev && next && prev !== next)
          || (prevBundle && nextBundle && prevBundle !== nextBundle)) {
        window.location.reload()
        return
      }
      // The gateway pushes every 5 s and each frame differs from the last only
      // in its counters. Storing such a frame replaces `status` and runs every
      // store subscriber for no visible change, hidden tabs included. Skip it
      // when nothing a surface reads has changed.
      const key = stableStatusKey(data)
      const held = getDashboard?.()
      const last = lastStoredRef.current
      if (held && last && held.connected && held.status != null && held.status === last.status && last.key === key) {
        return
      }
      dispatch(sseStatus(data))
      lastStoredRef.current = { key, status: getDashboard?.().status ?? null }
    },
  }), [dispatch, getDashboard])
}
