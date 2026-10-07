import { useCallback, useState } from 'react'

import { api } from '../../../api/client'
import { useAgents } from '../../../hooks/useAgents'
import { useAvailableModels } from '../../../hooks/useAvailableModels'
import { useFilteredDropdown } from '../../../hooks/useFilteredDropdown'
import type { AppDispatch } from '../../../store'
import { triggerRefresh } from '../../../store/dashboardSlice'

interface SessionRostersOptions {
  activeSlot: string | null
  activeSlotProject: string | undefined
  refreshTrigger: number
  dispatch: AppDispatch
}

/**
 * The agent and model rosters the composer's pickers offer for the active
 * session: this machine's catalog. Also the agent picker's filter state and its
 * "set as default" write.
 */
export function useSessionRosters({ activeSlot, activeSlotProject, refreshTrigger, dispatch }: SessionRostersOptions) {
  const { agents: installedAgents, choices: catalogChoices, defaultAgent } = useAgents(refreshTrigger, activeSlot ?? undefined, activeSlotProject)
  // The picker lists every catalog row (a member and a template of one name
  // are two rows). A roster source that exposes only the folded list -- one
  // row per name -- is still a complete, if namespace-blind, catalog.
  const effectiveAgents = catalogChoices ?? installedAgents
  const [defaultAgentFailed, setDefaultAgentFailed] = useState(false)
  // Promotes an agent to the global default. Set-only: clearing the default lives on
  // the Agent Templates page, where the control is labelled and the outcome is visible.
  // Refresh goes through the store's global trigger rather than local state, because
  // every open picker (this one, each split pane, the Templates page) reads the same
  // setting — a per-hook refresh would leave sibling pickers showing the old default.
  // api.setDefaultAgent is called defensively: component tests mock the api module
  // partially, so the method can be absent under test.
  const toggleDefaultAgent = useCallback((name: string) => {
    setDefaultAgentFailed(false)
    Promise.resolve(api.setDefaultAgent?.(name))
      .then(() => dispatch(triggerRefresh()))
      .catch(() => setDefaultAgentFailed(true))
  }, [dispatch])
  const { open: agentDropdown, setOpen: setAgentDropdown, filter: agentFilter, setFilter: setAgentFilter, dropdownRef: agentDropdownRef, inputRef: agentInputRef, filtered: filteredAgentsByName } = useFilteredDropdown(effectiveAgents)
  const filteredAgents = filteredAgentsByName
  const effectiveModels = useAvailableModels()
  return {
    installedAgents, defaultAgent, effectiveAgents,
    defaultAgentFailed, toggleDefaultAgent,
    agentDropdown, setAgentDropdown, agentFilter, setAgentFilter, agentDropdownRef, agentInputRef, filteredAgents,
    effectiveModels,
  }
}
