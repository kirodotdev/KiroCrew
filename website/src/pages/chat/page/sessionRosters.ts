import { useCallback, useMemo, useState } from 'react'

import { api } from '../../../api/client'
import type { KiroCrewAgent } from '../../../components/AgentSelector'
import { useAgents } from '../../../hooks/useAgents'
import { useAvailableModels } from '../../../hooks/useAvailableModels'
import { useFilteredDropdown } from '../../../hooks/useFilteredDropdown'
import { useRemoteCapabilities } from '../../../hooks/useRemoteCapabilities'
import type { ModelInfo } from '../../../providers/types'
import type { AppDispatch } from '../../../store'
import { triggerRefresh } from '../../../store/dashboardSlice'
import type { ChatSlot } from '../../../types'

interface SessionRostersOptions {
  activeSlot: string | null
  activeSlotProject: string | undefined
  refreshTrigger: number
  slots: ChatSlot[]
  dispatch: AppDispatch
}

/**
 * The agent and model rosters the composer's pickers offer for the active
 * session: this machine's catalog, or -- for a session bound to a peer crew --
 * the peer's, substituted rather than merged. Also the agent picker's filter
 * state and its "set as default" write.
 */
export function useSessionRosters({ activeSlot, activeSlotProject, refreshTrigger, slots, dispatch }: SessionRostersOptions) {
  const { agents: installedAgents, choices: catalogChoices, defaultAgent } = useAgents(refreshTrigger, activeSlot ?? undefined, activeSlotProject)
  // The picker lists every catalog row (a member and a template of one name
  // are two rows). A roster source that exposes only the folded list -- one
  // row per name -- is still a complete, if namespace-blind, catalog.
  const agentChoices = catalogChoices ?? installedAgents
  // Is this session bound to a peer crew for execution, and what does that crew
  // offer? Read once here and threaded into the shelf's pickers below, so every
  // control answers from one source rather than each deciding for itself.
  const remoteCrew = useRemoteCapabilities(slots.find(s => s.key === activeSlot))
  const effectiveAgents = useMemo<KiroCrewAgent[]>(() => {
    // Local: the full catalog, so a same-name member and template stay two
    // rows. Remote: the peer's own roster, which knows no namespaces.
    if (!remoteCrew.isRemote) return agentChoices
    // The peer's roster carries the four fields its picker renders. The rest of
    // KiroCrewAgent describes bindings that only mean something on the machine
    // that owns them (`kiro_agent`, `workspace`, `memory_store`), so they are
    // filled with the empty value rather than this machine's — a local workspace
    // path shown under a remote crew's name would be a straightforward lie.
    return (remoteCrew.capabilities?.agents ?? []).map(a => ({
      name: a.name,
      kiro_agent: '',
      workspace: '',
      memory_store: '',
      model: a.model || '',
      description: a.description,
      source: a.scope || 'remote',
    }))
  }, [remoteCrew.isRemote, remoteCrew.capabilities, agentChoices])
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
  // own catalog. No pick keeps the configured backend's list, as before.
  const activeSlotBackend = slots.find(s => s.key === activeSlot)?.acp_backend ?? null
  const localModels = useAvailableModels({ backend: activeSlotBackend })
  // A peer-bound session's shelf must offer the PEER's rosters. Both hooks above
  // read THIS machine same-origin, so a remote session left on them would list
  // crews and models that do not exist over there — accepted by the picker, then
  // refused on the first send. Substituted rather than merged: the union would let
  // the user pick a local-only model and could not say which side it came from.
  //
  // While the capability read is in flight the lists are EMPTY, not local: a brief
  // empty picker is honest, whereas briefly showing this machine's models for a
  // remote session invites exactly the wrong pick.
  const effectiveModels = useMemo<ModelInfo[]>(() => {
    if (!remoteCrew.isRemote) return localModels
    return (remoteCrew.capabilities?.models ?? []).map(m => ({
      name: m.model_name,
      description: m.description || m.display_name,
      contextWindow: m.context_window || undefined,
    }))
  }, [remoteCrew.isRemote, remoteCrew.capabilities, localModels])
  return {
    installedAgents, defaultAgent, remoteCrew, effectiveAgents,
    defaultAgentFailed, toggleDefaultAgent,
    agentDropdown, setAgentDropdown, agentFilter, setAgentFilter, agentDropdownRef, agentInputRef, filteredAgents,
    effectiveModels, activeSlotBackend,
  }
}
