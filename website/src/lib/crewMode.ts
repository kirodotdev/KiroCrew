/** Crew Mode: a UI over the existing conductor agent.
 *
 *  A chat is in Crew Mode exactly when it runs on CREW_MODE_AGENT, so there is
 *  no separate field anywhere: the composer switch is an ordinary agent switch
 *  between the default agent and the conductor (the agent the backend installs
 *  from `agent_files.CONDUCTOR_AGENT_FILENAME`).
 */

export const CREW_MODE_AGENT = 'kirocrew-conductor'

/** Whether `name` is the default agent: the switch is offered only there. The
 *  literal `kirocrew` counts only while no default agent is configured; with
 *  one configured, a chat on the `kirocrew` template is that template. */
export function isDefaultAgentName(name: string, defaultAgent: string | undefined): boolean {
  if (name === '') return true
  return defaultAgent ? name === defaultAgent : name === 'kirocrew'
}

/** Crew Mode needs a Persistent chat: an Incognito or Temporary session may not
 *  open sessions, so a conductor there could not conduct. */
export function memoryModeAllowsCrewMode(memoryMode: string | undefined): boolean {
  return (memoryMode ?? 'persistent') === 'persistent'
}
