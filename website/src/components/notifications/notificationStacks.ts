import type { Notification } from '../../types'
import { noteMember } from './CrewmateNoteFace'
import { KIND_META, DEFAULT_META, notePriority } from './notifMeta'

/** Day clears preserve approval notes: reading one does not resolve it.
 *  Its own Approve/Reject decision removes it after resolution. */
export function keepsOutOfDayClear(n: Notification): boolean {
  return n.kind === 'approval'
}

/**
 * The stack a note joins in the feed, or null when it stands alone.
 *
 * A producer's own `group_key` wins: it names the thing being reported (an
 * incident, a failing source). Without one, notes stack by who sent them, the
 * way macOS Notification Center stacks by app: the publishing crewmate, then
 * the cron job, then the channel narrowed to the session that raised it.
 *
 * An approval or a critical note without a `group_key` never stacks. An
 * approval is its own decision, and a deck shows only its head's priority, so
 * a critical failure followed by an ordinary note from the same sender would
 * be buried under the ordinary one.
 *
 * Persisted rows are untrusted, so every field is shape-checked before it
 * becomes part of a key.
 */
export function stackKeyOf(n: Notification): string | null {
  if (typeof n.group_key === 'string' && n.group_key) return `group:${n.group_key}`
  if (n.kind === 'approval' || notePriority(n) === 'critical') return null
  const member = noteMember(n)
  if (member) return `member:${member.slug}`
  if (typeof n.job_id === 'string' && n.job_id) return `cron:${n.job_id}`
  if (typeof n.channel === 'string' && n.channel) {
    return typeof n.session_key === 'string' && n.session_key
      ? `channel:${n.channel}:${n.session_key}`
      : `channel:${n.channel}`
  }
  return null
}

/**
 * The name an expanded stack's header row shows, like the app name macOS puts
 * above a stack: the crewmate who sent it, else the app that sent it, else the
 * kind of note ("Cron Job", "Agent"). The cards under it carry the specifics.
 */
export function stackLabelOf(n: Notification): string {
  const member = noteMember(n)
  if (member) return member.name
  if (typeof n.source === 'string' && n.source && n.source !== 'system') return n.source
  return (KIND_META[n.kind] || DEFAULT_META).label
}
