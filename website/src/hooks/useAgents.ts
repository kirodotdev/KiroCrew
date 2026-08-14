import { useState, useEffect, useRef } from 'react'
import { api } from '../api/client'
import type { KiroCrewAgent } from '../components/AgentSelector'

/**
 * @param sessionKey Chat-slot key whose project scope should apply. Omit on
 *   surfaces with no slot context; project-scoped agents are then excluded.
 */
export function useAgents(refreshTrigger: number, sessionKey?: string) {
  const [agents, setAgents] = useState<KiroCrewAgent[]>([])
  const [defaultAgent, setDefaultAgent] = useState('')
  const hasSynced = useRef(false)
  const lastKey = useRef<string | undefined>(undefined)

  useEffect(() => {
    let cancelled = false
    // A slot switch must not leave the PREVIOUS slot's roster selectable while
    // the new scope's fetch is in flight: a stale project agent picked in that
    // window would be stored against the new slot and reset its project.
    // Cleared only on key change — a same-slot refresh keeps the current list
    // to avoid flicker.
    if (lastKey.current !== sessionKey) {
      lastKey.current = sessionKey
      setAgents([])
    }
    const fetchAgents = () =>
      api.kirocrewAgents(sessionKey).then(d => {
        if (cancelled) return
        setAgents(d.agents || [])
        setDefaultAgent(d.default_agent || '')
      }).catch(() => {})

    if (!hasSynced.current) {
      hasSynced.current = true
      api.syncKirocrewAgents().then(fetchAgents).catch(fetchAgents)
    } else {
      fetchAgents()
    }

    return () => { cancelled = true }
  }, [refreshTrigger, sessionKey])

  return { agents, defaultAgent }
}
