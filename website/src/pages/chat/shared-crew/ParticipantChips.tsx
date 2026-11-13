/** Who has written in a conversation, on its session row and its Bot row
 *  (#35145): one initials chip per person, the creator first and then most recent
 *  activity (the gateway's `participants` order), past three people the rest
 *  folded into "+N". Initials come from `principalInitials`, as the presence
 *  chips' do; no avatar is ever fetched.
 *
 *  The chips sit inside a row that is itself the control, so none is a tab stop:
 *  each is a named image whose accessible name is the person's display name, and
 *  whose hover title adds the login. 12px tall so the row's 12px meta line keeps
 *  its box (`ROW_META_CLS`); colours are theme tokens only.
 *
 *  Shown only on a team Crew, the creator byline's rule: the session list gates
 *  it with `SharedCrewBylineContext`, the Bots roster with `useTeamAttribution`. */
import type { PublicPrincipal } from '../../../api/sharedCrew'
import { principalInitials, principalName } from '../../../api/sharedCrewUi'
import { useTeamAttribution } from '../../../hooks/sharedCrew/useSharedCrew'
import { fmtList, fmtNumber } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'

/** Visible chips before the rest fold into "+N", as the presence chips do. */
export const MAX_PARTICIPANT_CHIPS = 3

const CHIP_CLS = 'inline-flex items-center justify-center h-3 rounded-full text-[7px] leading-none font-semibold border border-bg bg-bg-hover shrink-0'

function nameOf(p: PublicPrincipal): string {
  return principalName(p) || i18nT('sharedCrew.unattributed')
}

/** The hover text: the display name, and the login when it says something more. */
export function participantTitle(p: PublicPrincipal): string {
  const name = nameOf(p)
  const login = (p.display.login || '').trim()
  return login && login !== name ? `${name} (${login})` : name
}

export function ParticipantChips({ participants, testId }: { participants: readonly PublicPrincipal[] | null | undefined; testId: string }) {
  const people = participants ?? []
  if (people.length === 0) return null
  const shown = people.slice(0, MAX_PARTICIPANT_CHIPS)
  const rest = people.slice(shown.length)
  return (
    <span
      role="group"
      aria-label={i18nT('sharedCrew.participants', { n: fmtNumber(people.length) })}
      className="inline-flex items-center gap-px shrink-0 font-normal"
      data-testid={testId}
    >
      {shown.map(p => (
        <span key={p.id} role="img" aria-label={nameOf(p)} title={participantTitle(p)} className={`${CHIP_CLS} w-3 text-text`}>
          <span aria-hidden>{principalInitials(p)}</span>
        </span>
      ))}
      {rest.length > 0 && (
        <span
          role="img"
          aria-label={i18nT('sharedCrew.participants_more', { n: fmtNumber(rest.length) })}
          title={fmtList(rest.map(participantTitle), { style: 'narrow' })}
          className={`${CHIP_CLS} px-0.5 text-muted`}
          data-testid={`${testId}-more`}
        >
          <span aria-hidden>{`+${fmtNumber(rest.length)}`}</span>
        </span>
      )}
    </span>
  )
}

/** A Bot row's chips: drawn only on a team Crew. The hook runs only for a row
 *  that has someone to show, so a roster with no participants reads nothing. */
export function TeamParticipantChips({ participants, testId }: { participants: readonly PublicPrincipal[] | null | undefined; testId: string }) {
  if (!participants || participants.length === 0) return null
  return <TeamGate participants={participants} testId={testId} />
}

function TeamGate({ participants, testId }: { participants: readonly PublicPrincipal[]; testId: string }) {
  const team = useTeamAttribution()
  return team ? <ParticipantChips participants={participants} testId={testId} /> : null
}
