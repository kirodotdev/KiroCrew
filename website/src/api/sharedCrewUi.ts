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
import { i18nT } from '../i18n/t'

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
export type PeopleSource = 'observed' | 'session_creator' | 'directory'

export interface PeopleRow {
  principal: PublicPrincipal
  sources: PeopleSource[]
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
  enterprise: {
    human_credentials: { state: 'unavailable', reason: 'not_configured' },
    individual_grants: { state: 'unavailable', reason: 'not_configured' },
    workload_identity: { state: 'unavailable', reason: 'not_configured' },
    crew_identity: { state: 'unavailable', reason: 'not_configured' },
    heartbeat: { state: 'unavailable', reason: 'not_configured' },
    inference_budget: { state: 'unavailable', reason: 'not_configured' },
    deployment_context: { state: 'unavailable', reason: 'not_configured' },
    human_sign_in: { state: 'unavailable', reason: 'not_configured' },
    revocation: { state: 'unavailable', reason: 'not_configured' },
    workload_verifier: { state: 'unavailable', reason: 'not_configured' },
    crew_registration: { state: 'unavailable', reason: 'not_configured' },
    directory: { state: 'unavailable', reason: 'not_configured' },
    channel_actors: { state: 'unavailable', reason: 'not_configured' },
  },
}

/** A capability the UI may lean on: `available` or `degraded`. */
export function capabilityUsable(state: CapabilityState | undefined): boolean {
  return state === 'available' || state === 'degraded'
}

export function isUnknownPrincipal(p: PublicPrincipal | null | undefined): boolean {
  return !p || p.kind === 'unknown' || p.id === UNKNOWN_PRINCIPAL_ID
}

/** The label to show for a principal, or `''` when the caller must say Unknown.
 *  A channel sender with no cached name reads as its channel's person ("Slack
 *  user"): attributed, so never Unattributed, and never their raw user id. */
export function principalName(p: PublicPrincipal | null | undefined): string {
  if (isUnknownPrincipal(p) || !p) return ''
  const label = (p.display.name || p.display.login || '').trim()
  if (label) return label
  const via = principalVia(p)
  return via ? i18nT('sharedCrew.channel_person_unnamed', { channel: channelName(via) }) : ''
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
  const via = principalVia(a as PublicPrincipal)
  return {
    id: a.id,
    kind,
    display: {
      name: typeof a.display?.name === 'string' ? a.display.name : '',
      login: typeof a.display?.login === 'string' ? a.display.login : '',
      captured_at: typeof a.display?.captured_at === 'number' ? a.display.captured_at : null,
    },
    ...(via ? { via } : {}),
  }
}

/** Channels whose senders are attributed (never admitted), and their names. */
const CHANNEL_NAMES: Record<string, string> = { slack: 'Slack' }

/** The channel a principal was seen through (`'slack'`), else `''`. Only a
 *  channel this build knows counts, so a stray value never draws a mark. */
export function principalVia(p: PublicPrincipal | null | undefined): string {
  const via = p?.via
  return typeof via === 'string' && via in CHANNEL_NAMES ? via : ''
}

/** The product name of a channel (`'Slack'`), for "via Slack". */
export function channelName(via: string): string {
  return CHANNEL_NAMES[via] ?? ''
}

/** Whether the People rows show anyone besides the viewer who proves a team
 *  path: the mode fallback's `othersSeen`. A Slack person is attributed only,
 *  so they never count (the server's `crew_mode.others_observed` agrees). */
export function othersSeenIn(rows: readonly { is_viewer?: boolean; principal: PublicPrincipal }[] | undefined): boolean {
  return (rows ?? []).some(row => !row.is_viewer && !principalVia(row.principal))
}

/** The id of a session's default owner: its recorded creator, or the viewer
 *  when the session has no known creator (a legacy session). Empty when
 *  neither is known yet. */
export function sessionOwnerId(attribution: SessionAttribution | null | undefined, viewerId: string | null | undefined): string {
  const creator = attribution?.creator
  if (creator && !isUnknownPrincipal(creator)) return creator.id
  return viewerId ?? ''
}

/** The sender a user message is labelled with, or `null` for no label. The
 *  session's default owner needs no label; any other participant, an Unknown
 *  author included, is named. Nothing is labelled until the owner is known. */
export function bylineFor(author: PublicPrincipal | null, ownerId: string): PublicPrincipal | null {
  if (!author || !ownerId) return null
  return author.id === ownerId ? null : author
}

/** The setup paths, in the order the People surface offers them. */
export const SETUP_PATH_ORDER: readonly SetupPath[] = ['tailscale', 'kiro_login', 'systems_manager']

/** Each setup path's availability; an older gateway that omits them reads as none. */
export function setupPathStates(capabilities: SharedCrewCapabilities): Record<SetupPath, SetupPathCapability> {
  const fallback: SetupPathCapability = { state: 'unavailable', reason: 'not_in_this_build' }
  const raw = capabilities.setup_paths
  return {
    tailscale: raw?.tailscale ?? { state: 'unavailable', reason: 'not_configured' },
    kiro_login: raw?.kiro_login ?? fallback,
    systems_manager: raw?.systems_manager ?? fallback,
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
