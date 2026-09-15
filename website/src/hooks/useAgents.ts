import { useState, useEffect, useRef, useCallback } from 'react'
import { api } from '../api/client'
import type { KiroCrewAgent } from '../components/AgentSelector'

/**
 * @param sessionKey Chat-slot key whose project scope should apply. Omit on
 *   surfaces with no slot context; project-scoped agents are then excluded.
 * @param projectDir The slot's current project directory. The server resolves
 *   project-scoped agents from it, so it is part of this fetch's identity, not
 *   just an input to it: pointing the SAME slot at a different project changes
 *   the roster without changing `sessionKey`. Omit on surfaces with no slot
 *   context (the roster is then global-only and cannot go stale this way).
 *
 * @returns `error` — the roster fetch FAILED, as distinct from an install that
 *   genuinely has one agent. The two used to be the same observation: the fetch
 *   swallowed its rejection and left `agents` empty, so every caller rendered a
 *   failed load as a legitimately short list (#5990). Callers that cannot
 *   otherwise recover must surface it and offer `reload`.
 * @returns `reload` — re-run the roster fetch. `refreshTrigger` cannot serve as
 *   the retry on a surface that passes a constant (the schedule form passes
 *   `0`), because the effect then never runs again for the life of the mount.
 *   The one-shot sync is NOT repeated: it is per-mount by design, and a fetch is
 *   what failed.
 * @returns `reloading` — a `reload` fetch is in flight. Without it a retry that
 *   fails AGAIN is invisible: `setError(true)` over an already-true value bails
 *   out of re-rendering, so the surface is pixel-identical after the click and
 *   the one recovery affordance looks broken during the very outage it exists
 *   for. Callers use it to make the attempt visibly complete.
 */
export function useAgents(refreshTrigger: number, sessionKey?: string, projectDir?: string) {
  const [agents, setAgents] = useState<KiroCrewAgent[]>([])
  const [defaultAgent, setDefaultAgent] = useState('')
  const [error, setError] = useState(false)
  const [reloading, setReloading] = useState(false)
  const [reloadTick, setReloadTick] = useState(0)
  const reload = useCallback(() => {
    setReloading(true)
    setReloadTick(t => t + 1)
  }, [])
  const syncOnce = useRef<Promise<unknown> | null>(null)
  const syncSettled = useRef(false)
  // The scope this roster belongs to, held as two refs rather than one joined
  // key: comparing the parts needs no delimiter, so no directory name can forge
  // a scope boundary.
  const lastKey = useRef<string | undefined>(undefined)
  const lastProject = useRef<string | undefined>(undefined)

  useEffect(() => {
    let cancelled = false
    // A scope switch must not leave the PREVIOUS scope's roster selectable while
    // the new scope's fetch is in flight: a stale project agent picked in that
    // window would be stored against the new slot and reset its project.
    // Cleared only on scope change — a same-scope refresh keeps the current list
    // to avoid flicker. The scope is (slot, project) because re-pointing one
    // slot at another project makes the old project's agents just as stale as a
    // slot switch does.
    if (lastKey.current !== sessionKey || lastProject.current !== projectDir) {
      lastKey.current = sessionKey
      lastProject.current = projectDir
      setAgents([])
      // The previous scope's verdict says nothing about this one.
      setError(false)
    }
    const fetchAgents = () =>
      api.kirocrewAgents(sessionKey).then(d => {
        if (cancelled) return
        setAgents(d.agents || [])
        setDefaultAgent(d.default_agent || '')
        setError(false)
        setReloading(false)
      }).catch(() => {
        // Still swallowed as far as throwing goes — a rejected roster fetch must
        // not break the surface that asked for it — but no longer silent: the
        // list is left as-is (a failed REFRESH keeps the roster it already had)
        // and the failure becomes readable state.
        if (cancelled) return
        setError(true)
        // Cleared on the failing path too, so a retry that fails again still
        // resolves visibly instead of leaving the caller pinned in "trying".
        setReloading(false)
      })

    // Sync runs ONCE per mount. Hold the promise rather than a "started" flag so
    // a scope change arriving while it is still in flight waits for it too:
    // `/api/agents/sync` writes AIM-installed agents into config.json, and the
    // global rows of `/api/agents` are read back from that config, so a fetch
    // that overtakes the sync stores a pre-sync roster which then sticks until
    // the next scope change or a remount. Setting a project right after load is
    // the common path here, so that window is reachable rather than theoretical.
    // A failed sync must not strand the roster: it still settles, and the fetch
    // proceeds against whatever config is already on disk.
    if (!syncOnce.current) {
      syncOnce.current = api.syncKirocrewAgents()
        .catch(() => {})
        .then(() => { syncSettled.current = true })
    }
    // Once settled, fetch on the spot — deferring an already-settled sync by a
    // microtask would delay every later scope change for no benefit.
    if (syncSettled.current) fetchAgents()
    else syncOnce.current.then(fetchAgents)

    return () => { cancelled = true }
  }, [refreshTrigger, sessionKey, projectDir, reloadTick])

  return { agents, defaultAgent, error, reload, reloading }
}
