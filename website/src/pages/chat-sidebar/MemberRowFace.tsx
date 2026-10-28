/**
 * The head of a crewmate's own thread row in the session list.
 *
 * That row is named after the crewmate (`memberThreadTitle`), and a click opens
 * it on the Members page. It used to carry a "Members page" chip on its first
 * line with the name on a second line. It now draws the crewmate's avatar (the
 * same `CrewAvatar` the Crewmates roster draws) and name ON the first line,
 * beside the time, so the row reads as WHO it is and the preview sits right
 * under it. The row's separate title line is hidden by a `:has()` rule on the
 * row's text column (in `SessionRow.tsx`) while this head is present.
 *
 * The face comes from the crewmates roster (`membersRosterQuery`, the Members
 * page's own query, invalidated by every `refresh` frame and crew save), keyed
 * on the thread's exact crew name. While that read is loading, when it failed,
 * and for a crewmate the roster no longer lists (deleted, renamed), the old chip
 * renders instead and the title line stays where it was, so the row never wears
 * a face nobody can open. A failed read shows no banner: the row still works,
 * and the next roster refetch draws the face.
 *
 * `MemberRowHead` calls the roster query, so render it only on a row that opens
 * on the Members page: every other row never registers an observer.
 */
import { useQuery } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'

import type { MemberRosterRow } from '../../api/client'
import { membersRosterQuery } from '../../api/membersQuery'
import CrewAvatar from '../../components/CrewAvatar'
import { ROW_TITLE_CLS } from '../../components/listShell'
import { i18nT } from '../../i18n/t'

/** Edge length of the face, matched to the title's line box. */
export const MEMBER_ROW_FACE_PX = 16

function useMemberRow(agent: string): MemberRosterRow | null {
  const { data } = useQuery(membersRosterQuery)
  const rows = Array.isArray(data) ? data : []
  return agent ? rows.find(r => r.name === agent) ?? null : null
}

function MembersPageChip() {
  return (
    <span
      className="shrink-0 inline-flex items-center gap-0.5 text-[10px] px-1 rounded bg-info-subtle text-info-text border border-info/40"
      data-testid="members-page-chip">
      <ExternalLink size={9} className="shrink-0" aria-hidden="true" />
      {i18nT('pages.chatSidebar.members_page_chip')}
    </span>
  )
}

/**
 * The first line's leading content on a row that opens on the Members page:
 * the crewmate's avatar and name when the roster lists it (`agent` is the
 * thread's crew name, '' for a row that is not a crewmate thread), else the
 * "Members page" chip. The avatar is a still frame unless the thread is mid-turn.
 */
export function MemberRowHead({ agent, name, tooltip, running }: {
  agent: string
  name: string
  tooltip?: string
  running: boolean
}) {
  const row = useMemberRow(agent)
  if (!row) return <MembersPageChip />
  return (
    <>
      <span
        data-member-head
        // The scroll peek and the row's title tests read `[data-session-title]`;
        // this span is the row's visible title while it is drawn.
        data-session-title
        data-testid="member-row-head"
        title={tooltip}
        className={`${ROW_TITLE_CLS} min-w-0 truncate font-semibold text-text`}
      >
        <span data-testid="member-row-face" aria-hidden="true" className="inline-flex align-middle mr-1 -mt-0.5">
          <CrewAvatar seed={row.name} avatar={row.avatar} size={MEMBER_ROW_FACE_PX} working={running ? 'subtle' : undefined} />
        </span>
        {name}
      </span>
      {/* The face replaces the visible chip; a screen reader keeps the destination
          in the row's content, outside the title text the scroll peek copies. */}
      <span className="sr-only" data-testid="members-page-sr">{i18nT('pages.chatSidebar.members_page_chip')}</span>
    </>
  )
}
