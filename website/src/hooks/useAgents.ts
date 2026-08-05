import { useState, useEffect, useRef } from 'react'
import { api } from '../api/client'
import type { KiroCrewAgent } from '../components/AgentSelector'

export function useAgents(refreshTrigger: number) {
  const [agents, setAgents] = useState<KiroCrewAgent[]>([])
  const [defaultAgent, setDefaultAgent] = useState('')
  const hasSynced = useRef(false)

  useEffect(() => {
    let cancelled = false
    const fetchAgents = () =>
      api.kirocrewAgents().then(d => {
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
  }, [refreshTrigger])

  return { agents, defaultAgent }
}
