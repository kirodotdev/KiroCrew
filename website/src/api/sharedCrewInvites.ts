/** In-Crew Tailscale invites (`/api/shared-crew/tailscale/*`).
 *
 *  The owner connects a Tailscale API access key once; the gateway keeps it and
 *  never sends it back. These types are what the status read returns. */
import { ApiError } from './apiError'

export type InviteStatus = 'pending' | 'accepted'

export interface TailscaleInvite {
  id: string
  kind: 'email' | 'link'
  email: string
  multi_use: boolean
  alias: string
  avatar_id: string
  created_at: number
  status: InviteStatus
  accepted_login: string
  /** Link invites only: the URL the owner copies. */
  invite_url?: string
}

export interface TailscaleMember {
  user_id: string
  login: string
  alias: string
  avatar_id: string
  profile_pic_url: string
  invite_id: string
  accepted_at: number
}

export interface TailscaleInvitesStatus {
  connected: boolean
  tailnet: string
  key_expires: string
  key_expired: boolean
  key_expires_soon: boolean
  admin_share_url: string
  admin_keys_url: string
  invites: TailscaleInvite[]
  members: TailscaleMember[]
  last_sync_at: number
  sync?: { admitted?: string[]; restart_required?: boolean; error?: string }
}

export interface CreateInvitesBody {
  emails: string[]
  multi_use: boolean
  alias: string
  avatar_id: string
}

/** The fixed, public admin pages. Never taken from a response. */
export const TAILSCALE_ADMIN_MACHINES_URL = 'https://login.tailscale.com/admin/machines'
export const TAILSCALE_ADMIN_KEYS_URL = 'https://login.tailscale.com/admin/settings/keys'

export const AVATAR_MAX_BYTES = 256 * 1024
export const AVATAR_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']

export function avatarUrl(avatarId: string): string {
  return '/api/shared-crew/tailscale/avatars/' + encodeURIComponent(avatarId)
}

/** The gateway's `code` from a failed call, or `''`. */
export function inviteErrorCode(err: unknown): string {
  if (!(err instanceof ApiError)) return ''
  try {
    const body = JSON.parse(err.body) as { code?: unknown }
    return typeof body.code === 'string' ? body.code : ''
  } catch {
    return ''
  }
}

/** Split a typed list of addresses on commas, spaces and new lines. */
export function parseEmails(raw: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const part of raw.split(/[\s,;]+/)) {
    const email = part.trim()
    if (!email || seen.has(email.toLowerCase())) continue
    seen.add(email.toLowerCase())
    out.push(email)
  }
  return out
}

export function looksLikeEmail(email: string): boolean {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)
}

export function looksLikeApiKey(key: string): boolean {
  return /^tskey-api-[A-Za-z0-9]+-[A-Za-z0-9]+$/.test(key.trim())
}

/** Read a picked file as base64 for the avatar upload, after the size and type check. */
export async function fileToBase64(file: File): Promise<string> {
  const buf = new Uint8Array(await file.arrayBuffer())
  let bin = ''
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000))
  return btoa(bin)
}
