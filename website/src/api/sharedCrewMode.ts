/**
 * The Crew's People/setup mode (`GET /api/shared-crew/mode`, owned by
 * `kiro_crew.shared_crew.crew_mode`) and the pure helpers the session list and
 * People surface read it through.
 *
 * Modes are mutually exclusive: `personal` (only the local operator, no team
 * path), `network` (Tailscale, SSM or a team Crew on the Kiro relay), `console`
 * (an organization Console manages access) and `invalid` (a team setup exists
 * but is refused). The connection sources (`sources`) are not: a Crew reports
 * each one at once, and People shows them side by side.
 * Nothing here decides access: a mode or a source only chooses what is shown.
 */
import type { CapabilityState, PublicPrincipal, SessionAttribution, SetupPath, SetupPathCapability, SharedCrewCapabilities } from './sharedCrew'
import { distinctPeopleCount, isUnknownPrincipal, personSource, principalName, setupPathStates, soleSource, type PeopleRow, type PersonSource } from './sharedCrewUi'
import { i18nT } from '../i18n/t'

export type CrewMode = 'personal' | 'network' | 'console' | 'invalid'

export interface CrewModeResponse {
  schema_version: number
  mode: CrewMode
  /** Keys the remembered list filter; stable for this Crew's data home. */
  crew_id: string
  viewer?: PublicPrincipal
  /** Every principal id that names the viewer (the owner writes under several). */
  viewer_ids?: string[]
  team_config: { state: 'not_configured' | 'valid' | 'invalid'; reason: string; capability: string; cause: string }
  /** A Console link the server validated (https, named host). Only in console mode. */
  console: { state: 'available' | 'not_offered' | 'invalid'; url: string }
  adapters: Partial<Record<SetupPath, SetupPathCapability>>
  workload_identity_ref: string
  /** The path the viewer came in on. Absent from an older gateway. */
  viewer_via?: ViewerVia
  /** The owner's one name (the backend's `owner.owner_name`: their signed-in
   *  e-mail, Tailscale name, or this machine's name), for "Only <owner> …" copy. */
  owner_label?: string
  /** Every connection source at once (`crew_mode.sources_view`). Absent from an
   *  older gateway, which `connectionSources` reads from the setup paths. */
  sources?: ConnectionSource[]
}

/** A way people reach this Crew. `organization` is a console your organization
 *  runs; `kiro_login` is this Crew shared as a team Crew over the Kiro relay. */
export type SourceId = 'tailscale' | 'ssm' | 'organization' | 'kiro_login'

export interface ConnectionSource {
  id: SourceId
  state: CapabilityState
  reason: string
  /** Distinct people who came in through the source. */
  people: number
}

export const SOURCE_ORDER: readonly SourceId[] = ['tailscale', 'ssm', 'organization', 'kiro_login']

/** Whether a source is on: available, or on but not reachable. */
export function sourceOn(source: ConnectionSource | undefined): boolean {
  return source?.state === 'available' || source?.state === 'degraded'
}

/** Each connection source, in `SOURCE_ORDER`. An older gateway that sends no
 *  `sources` gets them from its setup paths and its mode. When the People rows
 *  were read (*rows* not null), each card counts the distinct people in the list
 *  under its chip, so a card and the list agree; otherwise the gateway's count. */
export function connectionSources(resp: CrewModeResponse | null | undefined, capabilities: SharedCrewCapabilities, rows: readonly PeopleRow[] | null): ConnectionSource[] {
  const sent = new Map((resp?.sources ?? []).filter(s => SOURCE_ORDER.includes(s.id)).map(s => [s.id, s]))
  const paths = setupPathStates(capabilities)
  const consoleMode = resp?.mode === 'console'
  const base = SOURCE_ORDER.map((id): ConnectionSource => {
    const known = sent.get(id)
    if (known) return known
    if (id === 'organization') return { id, state: consoleMode ? 'available' : 'unavailable', reason: consoleMode ? '' : 'not_offered', people: 0 }
    if (id === 'kiro_login') return { id, state: 'unavailable', reason: 'not_configured', people: 0 }
    const path = resp?.adapters?.[id] ?? paths[id]
    return { id, state: path.state, reason: path.reason ?? '', people: 0 }
  })
  if (!rows) return base
  const fallback = rowSourceFallback(base)
  return base.map(source => ({
    ...source,
    people: distinctPeopleCount(rows.filter(row => personSource(row, fallback) === source.id)),
  }))
}

/** The chip a row with no provenance takes: the one source that is on. */
export function rowSourceFallback(sources: readonly ConnectionSource[]): PersonSource | null {
  return soleSource(sources.filter(sourceOn).map(s => s.id))
}

export type ViewerVia = 'kiro_login' | 'tailscale' | 'ssm' | 'local' | 'unknown'

const MODES: ReadonlySet<string> = new Set<CrewMode>(['personal', 'network', 'console', 'invalid'])

/** The mode to render from a mode body the route answered. The route never
 *  answers `null` (a missing route is a query error, which the caller renders as
 *  an error before reaching here); without a body the mode is read from what
 *  the Crew does report, never assumed personal when a team path or another
 *  person is visible. */
export function effectiveMode(resp: CrewModeResponse | null | undefined, fallback: { teamPath: boolean; othersSeen: boolean }): CrewMode {
  if (resp && MODES.has(resp.mode)) return resp.mode
  return fallback.teamPath || fallback.othersSeen ? 'network' : 'personal'
}

/** A personal Crew hides creator bylines and the All/Mine control. */
export function hidesTeamAttribution(mode: CrewMode): boolean {
  return mode === 'personal'
}

/** Whether the dashboard shows who did what: the creator filter, session and
 *  message bylines, decision attribution and presence. Every one of these is
 *  part of the Shared Crew preview, so all must hold: the preview is on, the
 *  Crew can attribute, its mode is read and not personal, and someone besides
 *  the owner has joined (`someoneJoined`). Until then the dashboard looks as it
 *  does for one person. */
export function showsTeamAttribution(s: { previewOn: boolean; attributes: boolean; mode: CrewMode | null; joined: boolean }): boolean {
  return s.previewOn && s.attributes && s.mode !== null && !hidesTeamAttribution(s.mode) && s.joined
}

/** The Console URL to open, re-checked here: https to a named host, else none.
 *  A console can manage access beside the other sources, so any mode may carry one. */
export function consoleUrl(resp: CrewModeResponse | null | undefined): string {
  if (!resp || resp.console?.state !== 'available') return ''
  const raw = resp.console.url
  try {
    const u = new URL(raw)
    if (u.protocol !== 'https:' || !u.hostname.includes('.') || u.username || u.password) return ''
    return u.toString()
  } catch {
    return ''
  }
}

const FILTER_KEY_PREFIX = 'kc.shared-crew.creator-filter.'
const PRINCIPAL_ID = /^p_[a-z0-9_]{1,64}$/

function storage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    return null
  }
}

/** One stored "Started by" key: `mine`, `unattributed` (`unknown` is its older
 *  spelling) or a person's principal id. `''` for anything else. */
function creatorFilterKey(value: unknown): string {
  if (value === 'mine' || value === 'unattributed') return value
  if (value === 'unknown') return 'unattributed'
  if (typeof value === 'string' && PRINCIPAL_ID.test(value)) return value
  return ''
}

/** The "Started by" keys last chosen on this Crew; empty is every session. The
 *  value is a JSON array; the single key the older one-choice filter stored
 *  reads as a one-key selection, and `all` or anything unrecognised as none. */
export function loadCreatorFilter(crewId: string): string[] {
  if (!crewId) return []
  let raw: string | null = null
  try {
    raw = storage()?.getItem(FILTER_KEY_PREFIX + crewId) ?? null
  } catch {
    raw = null
  }
  if (!raw) return []
  let parsed: unknown = raw
  if (raw.startsWith('[')) {
    try {
      parsed = JSON.parse(raw)
    } catch {
      return []
    }
  }
  const values = Array.isArray(parsed) ? parsed : [parsed]
  return [...new Set(values.map(creatorFilterKey).filter(Boolean))]
}

export function saveCreatorFilter(crewId: string, keys: Iterable<string>): void {
  if (!crewId) return
  try {
    const s = storage()
    if (!s) return
    const v = [...new Set([...keys].map(creatorFilterKey).filter(Boolean))]
    if (v.length === 0) s.removeItem(FILTER_KEY_PREFIX + crewId)
    else s.setItem(FILTER_KEY_PREFIX + crewId, JSON.stringify(v))
  } catch {
    // A full or blocked store only loses the remembered choice.
  }
}

/** The local dashboard token's subjects (`kiro_crew.shared_crew.owner.
 *  LOCAL_BOOTSTRAP_SUBJECTS`): the Crew's own operator, never a person's name. */
const LOCAL_OPERATOR_NAMES: ReadonlySet<string> = new Set(['local-app', 'local-startup'])

/** A person's name for display. The owner's principal carries this machine's
 *  name; a gateway that could not read one still sends the token subject, which
 *  reads as the Crew owner. A tailnet person always has a login, so a teammate
 *  whose name looks like a token subject keeps it. `''` when nothing names them. */
export function displayName(p: PublicPrincipal | null | undefined): string {
  const name = principalName(p)
  if (LOCAL_OPERATOR_NAMES.has(name) && !p?.display.login) return i18nT('sharedCrew.crew_owner')
  return name
}

/** The label for a session's creator: "You" for the verified viewer, a
 *  person's name, the trigger behind unattended work, or Unattributed. A legacy
 *  session (made before sharing existed) arrives with the Crew owner as its
 *  creator, so it reads as the owner; only a creator nobody can name reads as
 *  Unattributed. */
export function creatorDisplay(attribution: SessionAttribution, viewerId?: string | null): string {
  const creator = attribution.creator
  if (!isUnknownPrincipal(creator) && creator.kind === 'human') {
    if (viewerId && creator.id === viewerId) return i18nT('sharedCrew.you')
    return displayName(creator) || i18nT('sharedCrew.unattributed')
  }
  const trigger = attribution.trigger
  if (trigger) {
    const name = trigger.name.trim()
    switch (trigger.kind) {
      case 'scheduled': return name ? i18nT('sharedCrew.trigger_scheduled', { name }) : i18nT('sharedCrew.trigger_scheduled_unnamed')
      case 'webhook': return name ? i18nT('sharedCrew.trigger_webhook', { name }) : i18nT('sharedCrew.trigger_webhook_unnamed')
      default: return i18nT('sharedCrew.trigger_system')
    }
  }
  return principalName(creator) || i18nT('sharedCrew.unattributed')
}

/** Hover text for a creator byline. */
export function creatorTitle(attribution: SessionAttribution, viewerId?: string | null): string {
  const label = creatorDisplay(attribution, viewerId)
  const trigger = attribution.trigger
  if (trigger && attribution.creator.kind !== 'human') {
    const by = trigger.configured_by && !isUnknownPrincipal(trigger.configured_by) ? principalName(trigger.configured_by) : ''
    return by
      ? i18nT('sharedCrew.started_by_configured', { name: label, by })
      : i18nT('sharedCrew.started_by', { name: label })
  }
  const login = attribution.creator.display.login
  return login && login !== label && !isUnknownPrincipal(attribution.creator)
    ? i18nT('sharedCrew.created_by_with_login', { name: label, login })
    : i18nT('sharedCrew.created_by', { name: label })
}
