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
import type { AppDispatch } from '../../store'
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

export interface BundleReload {
  /** A `dashboard` status frame: reload on a new version or bundle, else
   *  store the status. */
  onDashboardStatus(data: StatusData): void
}

export function useBundleReload(dispatch: AppDispatch): BundleReload {
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
      dispatch(sseStatus(data))
    },
  }), [dispatch])
}
