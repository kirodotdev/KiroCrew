/** Crew Mode: a UI over a conductor agent.
 *
 *  Every conductor is Crew Mode: a chat is in Crew Mode exactly when it runs a
 *  conductor TEMPLATE (or the agent its Crew Mode stamp names), so the composer
 *  switch is an ordinary agent switch from the default agent to the conductor.
 *  WHICH conductor is the gateway's `agent.crew_mode_agent`; the gateway moves
 *  every Crew Mode chat onto it when it changes, and refuses any other switch of
 *  a Crew Mode chat (`409 crew_mode_locked`). The conductor list is the gateway's
 *  one set (`kiro_crew.crew_mode.conductor_agents`);
 *  it arrives with the agent catalog (`GET /api/agents/catalog`, read by
 *  `useAgents`), which records them here. Until a catalog lands, and on a gateway
 *  whose catalog carries no Crew Mode fields, no template is a conductor.
 */

/** The conductor Crew Mode runs while the gateway has not said otherwise. */
export const DEFAULT_CREW_MODE_AGENT = 'kirocrew-conductor'

// Empty until a catalog says the gateway runs Crew Mode: a chat restored on a
// conductor template before that read lands, or on a gateway without Crew Mode,
// is an ordinary chat, so no picker row, chord or memory switch treats it otherwise.
let conductors: ReadonlySet<string> = new Set()

/** Record the catalog's Crew Mode facts. Called by `useAgents` on every load.
 *
 *  A catalog that carries NEITHER field comes from a gateway without Crew Mode
 *  (one that predates it). No template is a conductor there: the picker, the
 *  agent chords and the memory switch keep every row and every behaviour they
 *  have today, since there is no Crew Mode for a hidden conductor to be reached
 *  through. A Crew Mode gateway sends both fields: the conductor set is the
 *  gateway's own (`conductor_agents`), never a client copy; a catalog that names
 *  the agent but no list counts that agent alone. */
export function setCrewModeCatalog(
  agent: string | undefined,
  conductorAgents: readonly string[] | undefined,
): void {
  if (agent === undefined && conductorAgents === undefined) {
    conductors = new Set()
    return
  }
  conductors = new Set([...(conductorAgents ?? []), agent || DEFAULT_CREW_MODE_AGENT])
}

/** Back to the state before any catalog landed. For tests, and for nothing else. */
export function resetCrewModeCatalog(): void {
  conductors = new Set()
}

/** Whether `name` is one of the gateway's conductors. */
export function isConductorAgent(name: string | null | undefined): boolean {
  return !!name && conductors.has(name)
}

/** Whether a session on `agent` in namespace `kind` is in Crew Mode: on any
 *  conductor, or on the agent its `stamp` names (a custom conductor the config no
 *  longer names, until the gateway moves the chat onto the configured one). */
export function runsCrewMode(agent: string | null | undefined, kind: string | null | undefined, stamp?: string | null): boolean {
  if (kind === 'member' || !agent) return false
  return conductors.has(agent) || (!!stamp && stamp === agent)
}

/** Crew Mode needs a Persistent chat: an Incognito or Temporary session may not
 *  open sessions, so a conductor there could not conduct. */
export function memoryModeAllowsCrewMode(memoryMode: string | undefined): boolean {
  return (memoryMode ?? 'persistent') === 'persistent'
}

/** Rows of a normal chat's agent picker. The rule it will carry: every conductor
 *  left out, since the composer's Crew Mode switch is the way onto one; crewmates
 *  kept whatever their name. On this part the rule is INERT -- every row passes
 *  through -- because the switch is not here yet and the picker is still the one
 *  way onto a conductor template. The part that adds the switch turns the rule
 *  on here, in this one function; the picker, the Split View pane picker and the
 *  keyboard cycle (`agentCycleTarget`) already read through it. */
export function withoutConductors<T extends { name: string; selection_kind?: string }>(choices: T[]): T[] {
  return choices
}

/** The person's own session list: a Crew Mode root's workers are left out while
 *  that root is in the list, since the root's Crew panel (the side panel's Crew
 *  tab) lists them; the part before this one wired every person-facing list
 *  through here with the drop inert, and this part, which adds that panel, turns
 *  it on. A worker whose root is closed keeps its row: no panel lists it, and its
 *  own approval or question must still reach the person. `rootsOnly` keeps just
 *  the roots, for the session filter's Crew Mode chip. */
export function crewModeView<T extends { key?: string; crew_root?: string; crew_mode_root?: boolean }>(rows: T[], rootsOnly = false): T[] {
  if (rootsOnly) return rows.filter(r => !!r.crew_mode_root)
  const listed = new Set<string>()
  for (const r of rows) if (r.key) listed.add(r.key)
  return rows.filter(r => !underListedRoot(r, key => listed.has(key)))
}

/** A worker whose root is a row of the list: the one row `crewModeView` drops. */
function underListedRoot(row: { crew_root?: string }, listed: (key: string) => boolean): boolean {
  return !!row.crew_root && listed(row.crew_root)
}

/** Whether opening `row` in the main pane hides the sessions list on a narrow
 *  desktop: only a Crew Mode worker the person's list does NOT show (its root is
 *  in `rows`), since that root's Crew panel is where it is listed and the two side
 *  by side crowd the chat. The same test `crewModeView` drops on, so a worker
 *  that is still a row -- its root closed -- never closes the list it was picked
 *  from. */
export function openingHidesSessionList(row: { crew_root?: string } | null | undefined, rows: readonly { key: string }[]): boolean {
  return !!row && underListedRoot(row, key => rows.some(r => r.key === key))
}

/** The rows the page may open on its own (a first visit, or after the open chat
 *  closed): never a crew member, which is its conductor's to show and is opened
 *  from its Crew panel or by the person. A crew member opened on purpose stays
 *  open; only the page's own pick skips it. */
export function autoOpenRows<T extends { crew_root?: string }>(rows: T[]): T[] {
  return rows.filter(s => !s.crew_root)
}

/** The agent the keyboard cycle lands on from `current`, `step` places along
 *  (`1` next, `-1` previous). The cycle skips every conductor template, so a
 *  chord can never move a normal chat onto a conductor and turn Crew Mode on by
 *  accident; the picker is the way onto one. `null` when there is nothing to
 *  cycle: an empty list, or a chat that already runs a conductor template, which
 *  has no agent picker to mirror. Crewmates are kept whatever their name. */
export function agentCycleTarget<T extends { name: string; selection_kind?: string }>(
  agents: T[], current: string, currentKind: string | undefined, step: 1 | -1, crewStamp?: string,
): string | null {
  // A Crew Mode chat (every conductor, stamp included) has no picker to mirror.
  if (runsCrewMode(current, currentKind, crewStamp)) return null
  const rows = agents.filter(a => a.selection_kind === 'member' || !isConductorAgent(a.name))
  if (rows.length === 0) return null
  const idx = rows.findIndex(a => a.name === current)
  const next = idx === -1 ? (step === 1 ? 0 : rows.length - 1) : (idx + step + rows.length) % rows.length
  return rows[next].name
}
