/**
 * Shared Crew browser-side shapes that are not frozen wire contracts, plus the
 * pure helpers the UI reads them through. The frozen contracts live in
 * `./sharedCrew` and are only imported here, never redefined.
 *
 * Every principal shown is one the server issued. Nothing here decides access:
 * a filter, a byline or a presence chip is discovery and display only.
 */
import {
  UNKNOWN_PRINCIPAL_ID,
  type AcceptedWork,
  type CapabilityState,
  type CapacityResponse,
  type MembershipLink,
  type PresenceEntry,
  type PublicPrincipal,
  type ReasonCode,
  type SessionAttribution,
  type SessionPage,
  type SetupPath,
  type SetupPathCapability,
  type SharedCrewCapabilities,
  type SharedDecision,
} from './sharedCrew'

/** One row of `GET /api/shared-crew/sessions`: a catalog row plus its attribution. */
export interface SharedCrewSessionItem {
  key: string
  title?: string
  agent?: string
  messages?: number
  modified?: string | number
  created?: string | number
  attribution?: SessionAttribution
}

/** `GET /api/shared-crew/sessions` answer: the frozen page plus the echoed viewer. */
export interface SharedCrewSessionPage extends SessionPage<SharedCrewSessionItem> {
  viewer?: PublicPrincipal
  creator?: string
}

/** Where a People row's evidence came from. Unknown values read as observed. */
export type PeopleSource = 'observed' | 'session_creator' | 'directory' | 'tailscale_share'

export interface PeopleRow {
  principal: PublicPrincipal
  sources: PeopleSource[]
  /** This machine's owner. */
  is_owner?: boolean
  provenance?: string
  first_seen_at?: number | null
  last_seen_at?: number | null
  devices?: number
  sessions_created?: number
  is_viewer?: boolean
  /** `null` when this Crew cannot report usage for the person. */
  usage?: { active: number; queued: number } | null
  /** The alias and avatar the owner gave an invited teammate, bound by the
   *  server to this row's verified tailnet login. Display only. */
  member?: { alias: string; avatar_id: string; profile_pic_url: string }
  /** The stable Tailscale user id, when this Crew knows it: the key the
   *  display route edits. Absent rows cannot be renamed here. */
  tailscale_user_id?: string
  /** Set while the person has a tab open, from live presence. */
  live?: { activity: 'viewing' | 'typing' | 'idle'; session_key: string; session_title: string }
}

/** `PUT /api/shared-crew/people/{user_id}/display`. */
export interface PersonDisplayBody {
  alias: string
  avatar_data_base64?: string
  clear_avatar?: boolean
}

export interface PersonDisplay {
  user_id: string
  alias: string
  avatar_id: string
  audited: boolean
}

/** `GET /api/shared-crew/people`. The directory is a separate, authoritative list. */
export interface PeopleResponse {
  schema_version: number
  viewer?: PublicPrincipal
  directory: { state: CapabilityState; people?: PeopleRow[] }
  observed_since?: number | null
  usage_available?: boolean
  people: PeopleRow[]
}

export interface PresenceResponse {
  type: 'shared_crew.presence'
  schema_version: number
  session_key: string
  entries: PresenceEntry[]
}

export interface DecisionsResponse {
  schema_version: number
  items: SharedDecision[]
}

export interface WorkListResponse {
  schema_version?: number
  items: AcceptedWork[]
}

/** A Shared Crew read that the gateway does not serve answers `null`, not an error. */
export type Maybe<T> = T | null

export const ALL_UNAVAILABLE: SharedCrewCapabilities = {
  schema_version: 1,
  attribution: 'unavailable',
  presence: 'unavailable',
  queueing: 'unavailable',
  containment: 'unavailable',
  containment_level: 'none',
  authoritative_directory: 'unavailable',
  membership_link: '',
  remote_actions: 'unavailable',
  protocols: [],
  reasons: {},
}

/** Whether the gateway runs the Shared Crew preview (`KIROCREW_SHARED_CREW_PREVIEW=1`).
 *  Only a literal `true` turns it on: a missing field, an older gateway or a
 *  failed read all keep the sharing surfaces hidden. */
export function sharedCrewPreviewOn(capabilities: SharedCrewCapabilities): boolean {
  return capabilities.shared_crew_preview === true
}

/** Whether anyone besides the Crew owner has joined: a non-owner row this Crew
 *  saw on an authorized request (`observed`) since it started. A row known only
 *  as a session's creator (a Slack sender, say), an invite not yet used or a
 *  directory entry has not joined. A teammate viewing counts, since they joined
 *  to be here. The observed list is in memory, so after a restart the team
 *  surfaces come back once a teammate connects again. */
export function someoneJoined(rows: readonly PeopleRow[] | null | undefined): boolean {
  return (rows ?? []).some(row => !row.is_owner && !isUnknownPrincipal(row.principal) && (row.sources ?? []).includes('observed'))
}

/** Whether an enterprise policy closes Tailscale invites on this gateway. */
export function invitesPinnedOff(capabilities: SharedCrewCapabilities): boolean {
  return capabilities.invites?.reason === 'governance_pinned_off'
}

/** A capability the UI may lean on: `available` or `degraded`. */
export function capabilityUsable(state: CapabilityState | undefined): boolean {
  return state === 'available' || state === 'degraded'
}

export function isUnknownPrincipal(p: PublicPrincipal | null | undefined): boolean {
  return !p || p.kind === 'unknown' || p.id === UNKNOWN_PRINCIPAL_ID
}

/** The label to show for a principal, or `''` when the caller must say Unknown. */
export function principalName(p: PublicPrincipal | null | undefined): string {
  if (isUnknownPrincipal(p) || !p) return ''
  return (p.display.name || p.display.login || '').trim()
}

/** One or two letters for a principal's chip; never fetched, never an avatar URL. */
export function principalInitials(p: PublicPrincipal | null | undefined): string {
  const name = principalName(p)
  if (!name) return '?'
  const parts = name.split(/[\s@._-]+/).filter(Boolean)
  const first = Array.from(parts[0] ?? '')[0] ?? '?'
  const second = parts.length > 1 ? Array.from(parts[1])[0] ?? '' : ''
  return (first + second).toUpperCase()
}

/** Read a message row's server-stamped author, or `null` when it carries none. */
export function messageAuthor(meta: Record<string, unknown> | undefined): PublicPrincipal | null {
  const raw = meta?.author
  if (!raw || typeof raw !== 'object') return null
  const a = raw as Partial<PublicPrincipal> & { display?: Partial<PublicPrincipal['display']> }
  if (typeof a.id !== 'string' || !a.id) return null
  const kind = a.kind === 'human' || a.kind === 'machine' || a.kind === 'system' ? a.kind : 'unknown'
  return {
    id: a.id,
    kind,
    display: {
      name: typeof a.display?.name === 'string' ? a.display.name : '',
      login: typeof a.display?.login === 'string' ? a.display.login : '',
      captured_at: typeof a.display?.captured_at === 'number' ? a.display.captured_at : null,
    },
  }
}

/** The id whose messages carry no byline: the verified viewer, so a reader's
 *  own messages stay unlabelled and everyone else's, the session creator's
 *  included, are named. Until the viewer read lands it is the session's
 *  recorded creator, so a creator's messages are not labelled as someone
 *  else's in the meantime. Empty when neither is known yet. */
export function bylineSelfId(
  viewer: PublicPrincipal | null | undefined,
  attribution: SessionAttribution | null | undefined,
): string {
  if (viewer && !isUnknownPrincipal(viewer)) return viewer.id
  const creator = attribution?.creator
  if (creator && !isUnknownPrincipal(creator)) return creator.id
  return ''
}

/** The sender a user message is labelled with, or `null` for no label. The
 *  message's own writer (`selfId`, from `bylineSelfId`) needs no label; any
 *  other participant, an Unknown author included, is named. Nothing is
 *  labelled until `selfId` is known. */
export function bylineFor(author: PublicPrincipal | null, selfId: string): PublicPrincipal | null {
  if (!author || !selfId) return null
  return author.id === selfId ? null : author
}

/** The setup paths, in the order the People surface offers them. */
export const SETUP_PATH_ORDER: readonly SetupPath[] = ['tailscale', 'kiro_login']

/** Each setup path's availability; an older gateway that omits them reads as none. */
export function setupPathStates(capabilities: SharedCrewCapabilities): Record<SetupPath, SetupPathCapability> {
  const raw = capabilities.setup_paths
  return {
    tailscale: raw?.tailscale ?? { state: 'unavailable', reason: 'not_configured' },
    kiro_login: raw?.kiro_login ?? { state: 'unavailable', reason: 'not_in_this_build' },
  }
}

/** True once any setup path is available, so the Crew has a shared-access path. */
export function sharedAccessConfigured(capabilities: SharedCrewCapabilities): boolean {
  const states = setupPathStates(capabilities)
  return SETUP_PATH_ORDER.some(path => states[path].state === 'available')
}

/**
 * Fold one work item into a list by `work_id`, keeping whichever copy has the
 * higher revision. A frame replayed after a reconnect, or an HTTP read that
 * lands after a newer frame, can never move an item backwards or duplicate it.
 */
export function mergeWork(list: readonly AcceptedWork[], next: AcceptedWork): AcceptedWork[] {
  const i = list.findIndex(w => w.work_id === next.work_id)
  if (i < 0) return [...list, next]
  if (list[i].revision >= next.revision) return list as AcceptedWork[]
  const copy = list.slice()
  copy[i] = next
  return copy
}

/** Same rule as {@link mergeWork} for shared decisions. */
export function mergeDecision(list: readonly SharedDecision[], next: SharedDecision): SharedDecision[] {
  const i = list.findIndex(d => d.decision_id === next.decision_id)
  if (i < 0) return [...list, next]
  if (list[i].revision >= next.revision) return list as SharedDecision[]
  const copy = list.slice()
  copy[i] = next
  return copy
}

/** Which scope a refusal or a queue is held by. */
export type LimitScope = 'principal' | 'crew' | null

const PERSONAL_REASONS: ReadonlySet<ReasonCode> = new Set<ReasonCode>(['principal_active_limit', 'principal_queue_full', 'session_create_rate'])
const CREW_REASONS: ReadonlySet<ReasonCode> = new Set<ReasonCode>(['crew_active_limit', 'crew_queue_full', 'storage_limit', 'containment_unavailable'])

export function limitScope(reason: ReasonCode | null | undefined, capacity?: CapacityResponse | null): LimitScope {
  if (capacity?.limiting_scope) return capacity.limiting_scope
  if (reason && PERSONAL_REASONS.has(reason)) return 'principal'
  if (reason && CREW_REASONS.has(reason)) return 'crew'
  return null
}

const KNOWN_REASONS: ReadonlySet<string> = new Set<ReasonCode>([
  'principal_active_limit', 'principal_queue_full', 'crew_active_limit', 'crew_queue_full',
  'session_create_rate', 'request_too_large', 'storage_limit', 'containment_unavailable',
  'queue_expired', 'run_deadline', 'runtime_error', 'grant_revoked', 'policy_changed', 'cancelled_by_user', 'access_removed',
  'stale_revision', 'already_resolved', 'idempotency_conflict', 'invalid_transition', 'not_found',
])

export function asReasonCode(code: unknown): ReasonCode | null {
  return typeof code === 'string' && KNOWN_REASONS.has(code) ? code as ReasonCode : null
}

/** A body is an `AcceptedWork` when it carries the fields the UI keys on. */
export function isAcceptedWork(value: unknown): value is AcceptedWork {
  if (!value || typeof value !== 'object') return false
  const w = value as Partial<AcceptedWork>
  return typeof w.work_id === 'string' && typeof w.state === 'string' && typeof w.revision === 'number'
}

/** Which access-management pointer the People surface may show. Never a URL. */
export function membershipPointer(link: MembershipLink | undefined): 'tailscale_admin' | 'console' | null {
  return link === 'tailscale_admin' || link === 'console' ? link : null
}
