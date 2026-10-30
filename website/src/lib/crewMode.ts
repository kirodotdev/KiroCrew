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
 *
 *  Crew Mode is a feature preview (`PREVIEW_CREW_MODE`, default off).
 *  `crewModeEnabled()` is the one read of it: every helper here consults it, and
 *  every surface that reads a row's Crew Mode stamps reads them through
 *  `crewModeRoot` / `crewRootOf`. Off, this module answers as it does for a
 *  gateway without Crew Mode, and the stamps read as absent.
 */
import { PREVIEW_CREW_MODE, readPreviewFlag } from '../utils/previewFlags'

/** Whether the Crew Mode preview is on for this device. The one read of the flag:
 *  `useCrewMode` subscribes to the same key for a re-render. */
export function crewModeEnabled(): boolean {
  return readPreviewFlag(PREVIEW_CREW_MODE)
}

/** Whether `row` is a Crew Mode root, read through the flag: the server's
 *  `crew_mode_root` stamp, or `false` while the preview is off. */
export function crewModeRoot(row: { crew_mode_root?: boolean } | null | undefined): boolean {
  return !!row?.crew_mode_root && crewModeEnabled()
}

/** The Crew Mode root `row` is a crew member of, read through the flag: the
 *  server's `crew_root` stamp, or `''` while the preview is off, so a crew member
 *  lists, opens and alerts as an ordinary chat. */
export function crewRootOf(row: { crew_root?: string | null } | null | undefined): string {
  return (row?.crew_root && crewModeEnabled()) ? row.crew_root : ''
}

/** The conductor Crew Mode runs while the gateway has not said otherwise. */
export const DEFAULT_CREW_MODE_AGENT = 'kirocrew-conductor'

/** The longest steer a person may type into a worker: the server's
 *  `MAX_SEND_MESSAGE_CHARS`, which the steer route and the worker's own composer
 *  send both refuse past (`message_too_long`). */
export const CREW_STEER_MAX_CHARS = 50_000

let configuredAgent = DEFAULT_CREW_MODE_AGENT
// Empty until a catalog says the gateway runs Crew Mode: a chat restored on a
// conductor template before that read lands, or on a gateway without Crew Mode,
// is an ordinary chat, so no picker row, chord or memory switch treats it otherwise.
let conductors: ReadonlySet<string> = new Set()
let gatewayHasCrewMode = false

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
  configuredAgent = agent || DEFAULT_CREW_MODE_AGENT
  if (agent === undefined && conductorAgents === undefined) {
    conductors = new Set()
    gatewayHasCrewMode = false
    return
  }
  gatewayHasCrewMode = true
  conductors = new Set([...(conductorAgents ?? []), configuredAgent])
}

/** Whether the gateway has said it runs Crew Mode (its catalog carried the Crew
 *  Mode fields). `false` before the first catalog read and on a gateway without
 *  Crew Mode, where every read that would narrow the person's view to Crew Mode's
 *  rules is left as it is today. */
export function gatewayRunsCrewMode(): boolean {
  return gatewayHasCrewMode && crewModeEnabled()
}

/** Back to the state before any catalog landed. For tests, and for nothing else. */
export function resetCrewModeCatalog(): void {
  configuredAgent = DEFAULT_CREW_MODE_AGENT
  conductors = new Set()
  gatewayHasCrewMode = false
}

/** The agent a chat switches to when Crew Mode is turned on. */
export function crewModeAgent(): string {
  return configuredAgent
}

/** Whether `name` is one of the gateway's conductors. */
export function isConductorAgent(name: string | null | undefined): boolean {
  return !!name && conductors.has(name) && crewModeEnabled()
}

/** Whether a session on `agent` in namespace `kind` runs a conductor TEMPLATE. A
 *  crewmate that carries a conductor's name is that crewmate, not a conductor. */
export function runsConductor(agent: string | null | undefined, kind: string | null | undefined): boolean {
  return kind !== 'member' && isConductorAgent(agent)
}

/** Whether a session on `agent` in namespace `kind` is in Crew Mode: on any
 *  conductor, or on the agent its `stamp` names (a custom conductor the config no
 *  longer names, until the gateway moves the chat onto the configured one). */
export function runsCrewMode(agent: string | null | undefined, kind: string | null | undefined, stamp?: string | null): boolean {
  if (!crewModeEnabled() || kind === 'member' || !agent) return false
  return conductors.has(agent) || (!!stamp && stamp === agent)
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

/** Rows of a normal chat's agent picker: every conductor left out, since the
 *  composer's Crew Mode switch (this part) is the way onto one; crewmates kept
 *  whatever their name. The picker and the Split View pane picker read through
 *  this one function; the keyboard cycle (`agentCycleTarget`) skips conductors on
 *  its own. On a gateway without Crew Mode no template is a conductor, so every
 *  row passes. */
export function withoutConductors<T extends { name: string; selection_kind?: string }>(choices: T[]): T[] {
  return choices.filter(c => c.selection_kind === 'member' || !isConductorAgent(c.name))
}

/** Every surface that can show a session's pending tool approval. Listed so one
 *  test walks them all; a new surface joins the list and the rule below. */
export const CREW_APPROVAL_SURFACES = [
  'conductor_card',       // the conductor's approval card stack (CrewApprovalCards)
  'worker_main_pane',     // the worker's own composer in the main pane
  'worker_split_pane',    // the worker's composer in a Split View pane
  'conductor_side_panel', // the conductor side panel's Approvals and attention tab
  'session_dashboards',   // All Dashboards' Needs you section
] as const
export type CrewApprovalSurface = typeof CREW_APPROVAL_SURFACES[number]

/** Whether *surface* may draw decision buttons (Approve, Reject, Trust) for
 *  *slot*'s pending tool approval. The one rule, for every surface: a Crew Mode
 *  worker's approval (a row with `crew_root`) is decided ONLY on its conductor's
 *  approval card; every other surface shows it as info with a way to that card.
 *  A session that is no worker keeps its usual controls everywhere. */
export function crewApprovalDecidableHere(
  slot: { crew_root?: string | null } | null | undefined, surface: CrewApprovalSurface,
): boolean {
  if (!crewRootOf(slot)) return true
  return surface === 'conductor_card'
}

/** The person's own session list: a Crew Mode root's workers are left out while
 *  that root is in the list, since the root's Crew panel (the side panel's Crew
 *  tab) lists them; the part before this one wired every person-facing list
 *  through here with the drop inert, and this part, which adds that panel, turns
 *  it on. A worker whose root is closed keeps its row: no panel lists it, and its
 *  own approval or question must still reach the person. `rootsOnly` keeps just
 *  the roots, for the session filter's Crew Mode chip. */
export function crewModeView<T extends { key?: string; crew_root?: string; crew_mode_root?: boolean }>(rows: T[], rootsOnly = false): T[] {
  if (rootsOnly) return rows.filter(r => crewModeRoot(r))
  if (!crewModeEnabled()) return rows
  const listed = new Set<string>()
  for (const r of rows) if (r.key) listed.add(r.key)
  return rows.filter(r => !underListedRoot(r, key => listed.has(key)))
}

/** A worker whose root is a row of the list: the one row `crewModeView` drops. */
function underListedRoot(row: { crew_root?: string }, listed: (key: string) => boolean): boolean {
  const root = crewRootOf(row)
  return !!root && listed(root)
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
  return rows.filter(s => !crewRootOf(s))
}

/** The agent the keyboard cycle lands on from `current`, `step` places along
 *  (`1` next, `-1` previous). The cycle skips every gateway conductor template
 *  whatever the preview flag says (the gateway does not read it), so a chord can
 *  never move a normal chat onto a conductor and turn Crew Mode on by accident;
 *  the picker is the way onto one. `null` when there is nothing to
 *  cycle: an empty list, a chat in Crew Mode (`runsCrewMode`), which has no agent
 *  picker to mirror, or a `locked` chat: the caller passes the row's RAW
 *  `crew_mode_root` stamp, set only on a chat the gateway refuses to move.
 *  Crewmates are kept whatever their name. */
export function agentCycleTarget<T extends { name: string; selection_kind?: string }>(
  agents: T[], current: string, currentKind: string | undefined, step: 1 | -1, crewStamp?: string, locked = false,
): string | null {
  // The raw `crew_mode_root` stamp is the gateway's own lock (`crew_mode_locked`),
  // preview on or off; with the preview on, every Crew Mode chat has no picker.
  if (locked || runsCrewMode(current, currentKind, crewStamp)) return null
  const rows = agents.filter(a => a.selection_kind === 'member' || !conductors.has(a.name))
  if (rows.length === 0) return null
  const idx = rows.findIndex(a => a.name === current)
  const next = idx === -1 ? (step === 1 ? 0 : rows.length - 1) : (idx + step + rows.length) % rows.length
  return rows[next].name
}
