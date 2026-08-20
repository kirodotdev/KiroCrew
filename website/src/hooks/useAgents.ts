import { useState, useEffect, useRef } from 'react'
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
 */
export function useAgents(refreshTrigger: number, sessionKey?: string, projectDir?: string) {
  const [agents, setAgents] = useState<KiroCrewAgent[]>([])
  const [defaultAgent, setDefaultAgent] = useState('')
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
    }
    const fetchAgents = () =>
      api.kirocrewAgents(sessionKey).then(d => {
        if (cancelled) return
        setAgents(d.agents || [])
        setDefaultAgent(d.default_agent || '')
      }).catch(() => {})

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
  }, [refreshTrigger, sessionKey, projectDir])

  return { agents, defaultAgent }
}
