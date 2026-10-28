/**
 * The face on a crewmate's own thread row in the session list.
 *
 * That row is named after the crewmate (`memberThreadTitle`), and a click opens
 * it on the Members page. It used to carry a "Members page" chip above the
 * title; it now wears the crewmate's avatar beside its name instead, the same
 * `CrewAvatar` the Crewmates roster draws, so the row reads as WHO it is.
 *
 * The face comes from the crewmates roster (`membersRosterQuery`, the Members
 * page's own query, invalidated by every `refresh` frame and crew save), keyed
 * on the thread's exact crew name. While that read is loading, when it failed,
 * and for a crewmate the roster no longer lists (deleted, renamed), no face is
 * drawn and the old chip stands in, so the row never wears a face nobody can
 * open and never loses its destination cue.
 *
 * Both components call the roster query, so render them only on a row that
 * opens on the Members page: every other row never registers an observer.
 */
import { useQuery } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'

import type { MemberRosterRow } from '../../api/client'
import { membersRosterQuery } from '../../api/membersQuery'
import CrewAvatar from '../../components/CrewAvatar'
import { i18nT } from '../../i18n/t'

/** Edge length of the face, matched to the title's line box. */
export const MEMBER_ROW_FACE_PX = 16

function useMemberRow(agent: string): MemberRosterRow | null {
  const { data } = useQuery(membersRosterQuery)
  const rows = Array.isArray(data) ? data : []
  return rows.find(r => r.name === agent) ?? null
}

/** The crewmate's avatar, leading the row's title, or nothing when the roster
 *  does not list it. A still frame unless the thread is mid-turn. */
export function MemberRowFace({ agent, running }: { agent: string; running: boolean }) {
  const row = useMemberRow(agent)
  if (!row) return null
  return (
    <span
      data-testid="member-row-face"
      aria-hidden="true"
      className="inline-flex align-middle mr-1 -mt-0.5 shrink-0"
    >
      <CrewAvatar seed={row.name} avatar={row.avatar} size={MEMBER_ROW_FACE_PX} working={running ? 'subtle' : undefined} />
    </span>
  )
}

/** The "Members page" chip, shown only when no face can be drawn. With a face,
 *  the destination stays in the row's content for a screen reader, but not in
 *  the title's text (the scroll peek copies that text verbatim). */
export function MembersPageFallbackChip({ agent }: { agent: string }) {
  const row = useMemberRow(agent)
  if (row) return <span className="sr-only" data-testid="members-page-sr">{i18nT('pages.chatSidebar.members_page_chip')}</span>
  return (
    <span
      className="shrink-0 inline-flex items-center gap-0.5 text-[10px] px-1 rounded bg-info-subtle text-info-text border border-info/40"
      data-testid="members-page-chip">
      <ExternalLink size={9} className="shrink-0" aria-hidden="true" />
      {i18nT('pages.chatSidebar.members_page_chip')}
    </span>
  )
}
