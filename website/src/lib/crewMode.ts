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
import { PREVIEW_CREW_MODE, PREVIEW_MATE_SESSIONS, readPreviewFlag } from '../utils/previewFlags'
import { tabStatus } from './sessionTabs'

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

/** The crewmate (mate) thread `row` was dispatched from, directly or through
 *  another session: the server's `mate_root` stamp, `''` off every mate tree.
 *  Not read through the Crew Mode flag: a mate's sessions are no Crew Mode crew,
 *  and their approvals and questions must reach the mate chat either way
 *  (docs/request-for-change/rfc-crewmate-workers-under-the-crewmate.md). */
export function mateRootOf(row: { mate_root?: string | null } | null | undefined): string {
  return row?.mate_root || ''
}

/** Whether the mate sessions preview is on for this device (`PREVIEW_MATE_SESSIONS`):
 *  a mate's sessions leave the person's lists for the mate's Sessions tab.
 *  `useMateSessionsHidden` subscribes to the same key for a re-render. */
export function mateSessionsHidden(): boolean {
  return readPreviewFlag(PREVIEW_MATE_SESSIONS)
}

/** The mate thread whose Sessions tab lists `row` instead of the person's lists:
 *  its `mate_root`, read through the mate sessions preview (`''` while it is off). */
export function hiddenMateRootOf(row: { mate_root?: string | null } | null | undefined): string {
  return mateSessionsHidden() ? mateRootOf(row) : ''
}

/** Navigation state asking the Mates page to open the named member's Profile
 *  card on Sessions once that member is active (a mate session's breadcrumb). */
export interface OpenMateSessionsState { openMateSessions: true }
export function wantsMateSessions(state: unknown): boolean {
  return !!state && typeof state === 'object' && (state as Partial<OpenMateSessionsState>).openMateSessions === true
}

/** The live mate threads among `rows` (slot `mode` `member`). Read over the FULL
 *  slot store: the chat page's own lists never hold a mate thread. */
export function liveMateThreads(rows: readonly { key?: string; mode?: string }[]): ReadonlySet<string> {
  return new Set(rows.filter(r => r.mode === 'member' && r.key).map(r => r.key as string))
}

/** The slot keys that are busy, each slot's own key plus the mate thread it was
 *  dispatched from (`mate_root`): a mate is working while ANY session in its tree
 *  is, as a waiting worker already badges the mate as needing you. A slot is busy
 *  while its turn runs, a child runs, or a child waits behind the spawn caps (the
 *  gateway refuses a new turn on it either way). */
export function busySlotKeys(
  rows: readonly { key?: string; running?: boolean; subagents_running?: boolean; mate_root?: string | null }[],
  subagentQueued: Readonly<Record<string, number>> | undefined,
): ReadonlySet<string> {
  const keys = new Set<string>()
  for (const r of rows) {
    if (!r.key || !(r.running || r.subagents_running || (subagentQueued?.[r.key] ?? 0) > 0)) continue
    keys.add(r.key)
    const mate = mateRootOf(r)
    if (mate) keys.add(mate)
  }
  return keys
}

/** The mate threads whose turn is parked on the person -- an approval or a
 *  question -- keyed by the mate thread's key. A session the mate dispatched
 *  (`mate_root`) that waits on the person marks its mate too, as a Crew Mode crew
 *  member's prompt badges its conductor: the mate chat is where it is answered.
 *  Keyed by the mate thread whether or not that tab is live, because the server
 *  keeps stamping a closed mate thread's sessions. The Mates page rows read
 *  this map. */
export function mateNeedsYouByKey(
  rows: readonly { key: string; mode?: string; mate_root?: string | null; running?: boolean; pending_approval?: boolean; needs_input?: boolean }[],
): Readonly<Record<string, boolean>> {
  const byKey: Record<string, boolean> = {}
  const waits = (sl: (typeof rows)[number]) => {
    const st = tabStatus(sl, [], sl.key)
    return st === 'permission' || st === 'question'
  }
  for (const sl of rows) {
    if (sl.mode === 'member') byKey[sl.key] = byKey[sl.key] || waits(sl)
  }
  for (const sl of rows) {
    const mate = mateRootOf(sl)
    if (mate && waits(sl)) byKey[mate] = true
  }
  return byKey
}

/** The conductor Crew Mode runs while the gateway has not said otherwise. */
export const DEFAULT_CREW_MODE_AGENT = 'kirocrew-team-lead'

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
// Whether any catalog has landed: `false` tells a catalog still on its way from one
// that said the gateway runs no Crew Mode.
let catalogKnown = false
// The catalog's default agent (`default_agent`), read by `newChatCrewModeAgent`,
// which runs outside any component.
let catalogDefaultAgent = ''

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
  defaultAgent?: string,
): void {
  configuredAgent = agent || DEFAULT_CREW_MODE_AGENT
  catalogKnown = true
  catalogDefaultAgent = defaultAgent || ''
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
  catalogKnown = false
  catalogDefaultAgent = ''
}

/** Whether an agent catalog has landed since load (or the last reset). */
export function crewModeCatalogKnown(): boolean {
  return catalogKnown
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

/** The conductor a just-created chat is switched to when the
 *  `dashboard.new_chat_crew_mode` setting is on, or `null` to leave it plain.
 *  Only a chat the composer would offer the Ultra Mode chip on
 *  (`useCrewModeSwitch`'s `crewModeEligible`): a gateway that runs Crew Mode, no
 *  chat mode, a local non-app Persistent chat, still on the default agent (so no
 *  pick and no folder pin chose another one), and a default agent that is not
 *  itself a conductor. */
export function newChatCrewModeAgent(slot: {
  agent?: string; agent_kind?: string; mode?: string; memory_mode?: string; instance_id?: string; origin?: string
}): string | null {
  if (!gatewayRunsCrewMode() || slot.mode || slot.instance_id || slot.origin === 'app') return null
  if (!memoryModeAllowsCrewMode(slot.memory_mode) || runsConductor(catalogDefaultAgent, 'template')) return null
  if (slot.agent_kind === 'member' || !isDefaultAgentName(slot.agent ?? '', catalogDefaultAgent)) return null
  return configuredAgent
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
  'sidebar_hover_card',   // the Sessions sidebar's needs-me hover card
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
export function crewModeView<T extends { key?: string; mode?: string; crew_root?: string; crew_mode_root?: boolean; mate_root?: string }>(
  rows: T[], rootsOnly = false, mateThreads?: ReadonlySet<string>,
): T[] {
  if (rootsOnly) return rows.filter(r => crewModeRoot(r))
  // A mate's session leaves the list while its mate thread is live (the mate's
  // Sessions tab lists it), behind its own preview, before the Crew Mode return so
  // that branch is unchanged. `mateThreads` is the full store's
  // (`liveMateThreads`); without it, the mate threads among `rows`.
  if (mateSessionsHidden()) {
    const mates = mateThreads ?? liveMateThreads(rows)
    rows = rows.filter(r => { const mate = mateRootOf(r); return !mate || !mates.has(mate) })
  }
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
 *  closed): never a mate's session while the mate sessions preview is on, and never
 *  a crew member, which is its conductor's to show and is opened
 *  from its Crew panel or by the person. A crew member opened on purpose stays
 *  open; only the page's own pick skips it. */
export function autoOpenRows<T extends { crew_root?: string; mate_root?: string }>(rows: T[]): T[] {
  return rows.filter(s => !crewRootOf(s) && !hiddenMateRootOf(s))
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
