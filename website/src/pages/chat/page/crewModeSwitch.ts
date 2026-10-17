/**
 * Crew Mode on the composer (CrewModeChip): whether the switch is offered for
 * this chat, whether it is on, and the toggle.
 *
 * A UI over the existing conductor. A chat is in Crew Mode exactly when it runs
 * on CREW_MODE_AGENT (lib/crewMode), so flipping it is the ordinary agent switch
 * and there is no other Crew Mode state. The composer exists only for an open
 * session, so this reads the session itself, never a pending pick.
 */
import { useCallback } from 'react'
import type { ChatSlot } from '../../../types'
import { CREW_MODE_AGENT, isDefaultAgentName, memoryModeAllowsCrewMode } from '../../../lib/crewMode'

/** Crew Mode is the conductor TEMPLATE; a crewmate that happens to carry the
 *  same name is that crewmate, not Crew Mode. */
function isCrewModeAgent(name: string | undefined, kind: string | undefined): boolean {
  return name === CREW_MODE_AGENT && kind !== 'member'
}

export function useCrewModeSwitch({ currentSlot, defaultAgent, switchAgent }: {
  currentSlot: ChatSlot | undefined
  /** The catalog's default template: the agent a plain chat runs on. */
  defaultAgent: string
  switchAgent: (agentName: string, kind?: 'member' | 'template') => Promise<void>
}) {
  // Offered on a plain local, non-app, Persistent chat that runs on the default
  // template (a plain session is never a crewmate) or already on the conductor.
  // Incognito and Temporary chats are left out: such a session may not open
  // sessions, so a conductor there could not conduct. With the conductor itself
  // configured as the default agent there is nothing to switch back to, so the
  // switch is not offered at all.
  const crewModeEligible = !!currentSlot && defaultAgent !== CREW_MODE_AGENT && !currentSlot.mode && !currentSlot.instance_id && currentSlot.origin !== 'app'
    && memoryModeAllowsCrewMode(currentSlot.memory_mode)
    && (isCrewModeAgent(currentSlot.agent, currentSlot.agent_kind)
      || (currentSlot.agent_kind !== 'member' && currentSlot.agent !== CREW_MODE_AGENT && isDefaultAgentName(currentSlot.agent ?? '', defaultAgent)))
  const crewModeOn = crewModeEligible && isCrewModeAgent(currentSlot?.agent, currentSlot?.agent_kind)
  // Both directions name the template namespace: a bare name would be read as
  // a crewmate whenever the slot's kind is unset. Off sends an empty name and
  // the server stamps the configured default template, so it is right even
  // while the agent catalog is loading or unavailable (`defaultAgent` is '').
  const toggleCrewMode = useCallback(() => {
    if (crewModeOn) void switchAgent('', 'template')
    else void switchAgent(CREW_MODE_AGENT, 'template')
  }, [crewModeOn, switchAgent])
  return { crewModeEligible, crewModeOn, toggleCrewMode }
}
