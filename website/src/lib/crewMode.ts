/** Crew Mode: a UI over a conductor agent.
 *
 *  A chat is in Crew Mode exactly when it runs a conductor TEMPLATE, so there is
 *  no separate field anywhere: the composer switch is an ordinary agent switch
 *  from the default agent to the conductor. WHICH conductor is the gateway's
 *  `agent.crew_mode_agent`, and which agents count as conductors is its one list
 *  (`kiro_crew.crew_mode.conductor_agents`); both arrive with the agent catalog
 *  (`GET /api/agents/catalog`, read by `useAgents`), which records them here.
 *  Until the catalog lands the shipped conductor stands in.
 */

/** The conductor Crew Mode runs while the gateway has not said otherwise. */
export const DEFAULT_CREW_MODE_AGENT = 'kirocrew-conductor'

let configuredAgent = DEFAULT_CREW_MODE_AGENT
let conductors: ReadonlySet<string> = new Set([DEFAULT_CREW_MODE_AGENT])

/** Record the catalog's Crew Mode facts. Called by `useAgents` on every load. */
export function setCrewModeCatalog(agent: string | undefined, conductorAgents: readonly string[] | undefined): void {
  configuredAgent = agent || DEFAULT_CREW_MODE_AGENT
  conductors = new Set([...(conductorAgents ?? []), configuredAgent])
}

/** The agent a chat switches to when Crew Mode is turned on. */
export function crewModeAgent(): string {
  return configuredAgent
}

/** Whether `name` is one of the gateway's conductors. */
export function isConductorAgent(name: string | null | undefined): boolean {
  return !!name && conductors.has(name)
}

/** Whether a session on `agent` in namespace `kind` runs a conductor TEMPLATE. A
 *  crewmate that carries a conductor's name is that crewmate, not a conductor. */
export function runsConductor(agent: string | null | undefined, kind: string | null | undefined): boolean {
  return kind !== 'member' && isConductorAgent(agent)
}

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

/** Rows of a normal chat's agent picker: every conductor template left out, since
 *  Crew Mode is the way into one. Crewmates are kept whatever their name. */
export function withoutConductors<T extends { name: string; selection_kind?: string }>(choices: T[]): T[] {
  return choices.filter(c => c.selection_kind === 'member' || !isConductorAgent(c.name))
}

/** The person's own session list: a Crew Mode root's workers are left out (the
 *  side panel lists them under their root). `rootsOnly` keeps just the roots,
 *  for the session filter's Crew Mode chip. */
export function crewModeView<T extends { crew_root?: string; crew_mode_root?: boolean }>(rows: T[], rootsOnly = false): T[] {
  return rows.filter(r => (rootsOnly ? !!r.crew_mode_root : !r.crew_root))
}

/** The agent the keyboard cycle lands on from `current`, `step` places along
 *  (`1` next, `-1` previous), over the same conductor-free list the chat picker
 *  shows, so a chord can never move a normal chat onto a conductor. `null` when
 *  there is nothing to cycle: an empty list, or a chat that already runs a
 *  conductor template, which has no agent picker to mirror. */
export function agentCycleTarget<T extends { name: string; selection_kind?: string }>(
  agents: T[], current: string, currentKind: string | undefined, step: 1 | -1,
): string | null {
  if (runsConductor(current, currentKind)) return null
  const rows = withoutConductors(agents)
  if (rows.length === 0) return null
  const idx = rows.findIndex(a => a.name === current)
  const next = idx === -1 ? (step === 1 ? 0 : rows.length - 1) : (idx + step + rows.length) % rows.length
  return rows[next].name
}
