/** Who may open this Crew over Tailscale (`/api/shared-crew/access`).
 *
 *  Tailscale access rules govern the direct link by default. With local
 *  authorization on, the gateway also checks the computed access set (owner,
 *  accepted invites, approvals and carried-over entries). These routes are
 *  owner-only; approval and removal writes require local authorization. */

export interface WaitingPerson {
  user_id: string
  login: string
  display_name: string
  /** The person's Tailscale profile picture (https only), display only. */
  avatar_url: string
  node: string
  first_seen: number
  last_seen: number
  attempts: number
}

export interface ApprovedPerson {
  user_id: string
  login: string
  display_name: string
  avatar_url: string
  first_seen: number
  approved_at: number
  approved_by: string
}

export interface HostPower {
  state: 'available' | 'unavailable'
  reason: string
  backend: string
  laptop: boolean
  laptop_reason: string
}

export interface FirstShare {
  /** True until the owner acknowledged the warning, while nobody else has access. */
  needs_ack: boolean
  /** Sessions teammates will see; `null` when the count could not be read. */
  sessions: number | null
  /** Crew memory items teammates will see; `null` when unknown. */
  memory_items: number | null
  host: HostPower
}

export interface AccessView {
  readable: boolean
  waiting: WaitingPerson[]
  approved: ApprovedPerson[]
  /** Entries the first start carried over from the old config allowlist. */
  seeded: string[]
  first_share_ack: boolean
  first_share: FirstShare
  /** False: Tailscale alone decides, so Approve, Remove and the waiting list are hidden. */
  local_authorization?: boolean
}

/** The header the dashboard sends after its own dialog, for an action that cannot be undone. */
export const CONFIRM_IRREVERSIBLE_HEADER = { 'X-Kirocrew-Confirm': 'irreversible' } as const

/** A picture URL the browser may load: plain https, no credentials. Anything else shows initials. */
export function safeAvatarUrl(url: string | undefined): string {
  if (!url || !url.startsWith('https://')) return ''
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'https:' && !parsed.username && !parsed.password ? parsed.href : ''
  } catch {
    return ''
  }
}

export function initials(name: string): string {
  return (name.trim()[0] ?? '?').toUpperCase()
}
