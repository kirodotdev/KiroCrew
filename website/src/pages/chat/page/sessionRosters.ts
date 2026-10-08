import { useCallback, useState } from 'react'

import { modelsBackendFor } from '../../../api/acpBackend'
import { api } from '../../../api/client'
import { useAgents } from '../../../hooks/useAgents'
import { useAvailableModels } from '../../../hooks/useAvailableModels'
import { useFilteredDropdown } from '../../../hooks/useFilteredDropdown'
import type { AppDispatch } from '../../../store'
import { triggerRefresh } from '../../../store/dashboardSlice'
import type { ChatSlot } from '../../../types'

interface SessionRostersOptions {
  activeSlot: string | null
  activeSlotProject: string | undefined
  refreshTrigger: number
  /** The slot list, read for the active chat's backend pick. */
  slots: ChatSlot[]
  dispatch: AppDispatch
}

/**
 * The agent and model rosters the composer's pickers offer for the active
 * session: this machine's catalog. Also the agent picker's filter state and its
 * "set as default" write.
 */
export function useSessionRosters({ activeSlot, activeSlotProject, refreshTrigger, slots, dispatch }: SessionRostersOptions) {
  const { agents: installedAgents, displayAgents, choices: catalogChoices, defaultAgent } = useAgents(refreshTrigger, activeSlot ?? undefined, activeSlotProject)
  // What the chat sidebar tints its rows from: the last LOADED roster, so a
  // session switch (which empties `installedAgents` until the new slot's fetch
  // lands) does not flash every agent label to muted and back. Falls back to
  // the scoped list for roster sources that expose only `agents`.
  const sidebarAgents = displayAgents ?? installedAgents
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
  // The active chat's own backend pick keys the model list: a backend serves its
  // own catalog. No pick keeps the configured backend's list, as before. A degraded
  // pick is no longer selectable and the gateway runs the chat on Kiro, so Kiro's
  // list is the one that applies; asking for the pick's would be refused every poll.
  const activeSlotRow = slots.find(s => s.key === activeSlot)
  const activeSlotBackend = activeSlotRow?.acp_backend ?? null
  const effectiveModels = useAvailableModels({ backend: modelsBackendFor(activeSlotRow) })
  return {
    installedAgents, sidebarAgents, defaultAgent, effectiveAgents,
    defaultAgentFailed, toggleDefaultAgent,
    agentDropdown, setAgentDropdown, agentFilter, setAgentFilter, agentDropdownRef, agentInputRef, filteredAgents,
    effectiveModels, activeSlotBackend,
  }
}
