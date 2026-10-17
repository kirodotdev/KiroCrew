/**
 * The crewmate a notification came from, drawn on the note: its avatar in the
 * icon slot and its display label beside the title.
 *
 * The gateway stamps `member: {slug, name}` from the PUBLISHING session (a
 * crewmate's pinned DM session), so the note itself only names the crewmate.
 * The face and label come from the crewmates roster (`membersRosterQuery`, the
 * Crewmates page's own query, which the gateway's `refresh` frame and every crew
 * save invalidate), keyed on the row with the note's slug AND exact name. A
 * crewmate no longer on the roster draws nothing, and the host's
 * kind icon stands in, so a deleted crewmate's old notes read as ordinary
 * agent notes rather than wearing a face nobody can open. A failed roster
 * read is reported once per surface by `CrewmateRosterError`.
 *
 * The components here call the roster query, so render them only where some
 * note's `noteMember` is non-null: a surface without one never touches the
 * query (or needs a QueryClient).
 */
import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { RotateCw } from 'lucide-react'

import type { MemberRosterRow } from '../../api/client'
import { membersRosterQuery } from '../../api/membersQuery'
import { crewDisplayName } from '../AgentSelector'
import CrewAvatar from '../CrewAvatar'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'
import { i18nT } from '../../i18n/t'
import type { Notification } from '../../types'

export interface NoteMember { slug: string; name: string }

/** The note's crewmate, or null. A persisted row is untrusted, so the shape is
 *  checked here rather than at each render site. */
export function noteMember(n: Pick<Notification, 'member'>): NoteMember | null {
  const m: unknown = n.member
  if (!m || typeof m !== 'object') return null
  const { slug, name } = m as Record<string, unknown>
  return typeof slug === 'string' && slug && typeof name === 'string' && name ? { slug, name } : null
}

function useCrewmateRoster() {
  const { data, isError, refetch, isFetching } = useQuery(membersRosterQuery)
  return { rows: Array.isArray(data) ? data : [], failed: isError, retry: refetch, retrying: isFetching }
}

function useCrewmateRow(member: NoteMember): MemberRosterRow | null {
  const { rows } = useCrewmateRoster()
  // Slug AND exact name: slugification is lossy, so two crew names can share
  // a slug, and the note was attributed to the binding's exact name.
  return rows.find(r => r.slug === member.slug && r.name === member.name) ?? null
}

/**
 * The roster read's failure, once per surface that shows crewmate notes (the
 * feed, the detail panel, the live banner). The faces fall back to the kind icon on their own,
 * so without this a failed read would look like "no crewmate sent these".
 */
export function CrewmateRosterError({ className = '' }: { className?: string }) {
  const { failed, retry, retrying } = useCrewmateRoster()
  if (!failed) return null
  return (
    <div className={`flex items-center gap-2 flex-wrap ${className}`}>
      {/* No hand-off: the bell feed and detail panel are overlays over whatever
          page the reader was on, including one holding an unsaved draft, and
          the hand-off navigates to the chat without passing `useGuardedLeave`. */}
      <ErrorNotice
        variant="inline"
        className="flex-1 min-w-0"
        message={i18nT('components.notifications.crewmateNoteFace.roster_load_failed')}
        testId="notification-crewmate-roster-error"
      />
      <Btn onClick={() => void retry()} disabled={retrying} data-testid="notification-crewmate-roster-retry">
        <RotateCw className="lucide-inline" aria-hidden />
        {i18nT('components.notifications.crewmateNoteFace.retry')}
      </Btn>
    </div>
  )
}

/** The crewmate's avatar, or `fallback` while the roster loads or when the
 *  crewmate is gone. */
export function CrewmateNoteFace({ member, size, fallback }: { member: NoteMember; size: number; fallback: ReactNode }) {
  const row = useCrewmateRow(member)
  if (!row) return <>{fallback}</>
  return (
    <span data-testid="notification-crewmate-face" title={crewDisplayName(row)} className="inline-flex shrink-0">
      <CrewAvatar seed={row.name} avatar={row.avatar} size={size} />
    </span>
  )
}

/** The crewmate's display label, or nothing while the roster loads or when the
 *  crewmate is gone. */
export function CrewmateNoteName({ member, className = '' }: { member: NoteMember; className?: string }) {
  const row = useCrewmateRow(member)
  if (!row) return null
  return <span data-testid="notification-crewmate-name" className={className}>{crewDisplayName(row)}</span>
}
