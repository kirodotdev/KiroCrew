/** Who has written in a conversation, on its session row and its Bot row:
 *  one initials chip per person, the creator first and then most recent
 *  activity (the gateway's `participants` order), past three people the rest
 *  folded into "+N". Initials come from `principalInitials`, as the presence
 *  chips' do; no avatar is ever fetched.
 *
 *  The chips sit inside a row that is itself the control, so none is a tab stop:
 *  each is a named image whose accessible name is the person's display name, and
 *  whose hover title adds the login. 12px tall so the row's 12px meta line keeps
 *  its box (`ROW_META_CLS`), with 10px letters, the dashboard's minimum; colours
 *  are theme tokens only. The "+N" also counts the people the gateway's capped
 *  list dropped (`participants_dropped`).
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

const CHIP_CLS = 'inline-flex items-center justify-center h-3 min-w-3 px-[3px] rounded-full text-[10px] leading-none font-semibold bg-bg-hover shrink-0'

function nameOf(p: PublicPrincipal): string {
  return principalName(p) || i18nT('sharedCrew.unattributed')
}

/** The hover text: the display name, and the login when it says something more. */
export function participantTitle(p: PublicPrincipal): string {
  const name = nameOf(p)
  const login = (p.display.login || '').trim()
  return login && login !== name ? `${name} (${login})` : name
}

interface ChipsProps {
  participants: readonly PublicPrincipal[] | null | undefined
  /** People past the gateway's list cap, counted into "+N" by number only. */
  dropped?: number
  /** A person the row already names (the session row's creator byline): left
   *  out of the chips, which then read as the others who wrote here. */
  excludeId?: string
  testId: string
}

export function ParticipantChips({ participants, dropped = 0, excludeId = '', testId }: ChipsProps) {
  const people = (participants ?? []).filter(p => !excludeId || p.id !== excludeId)
  if (people.length === 0 && dropped <= 0) return null
  const shown = people.slice(0, MAX_PARTICIPANT_CHIPS)
  const rest = people.slice(shown.length)
  const more = rest.length + Math.max(0, dropped)
  // The group's name says what the chips are, on hover as well as to a screen
  // reader: everyone who wrote, or the others when the row names one already.
  const label = i18nT(excludeId ? 'sharedCrew.participants_more' : 'sharedCrew.participants', { n: fmtNumber(people.length + Math.max(0, dropped)) })
  return (
    <span
      role="group"
      aria-label={label}
      title={label}
      className="inline-flex items-center gap-px shrink-0 font-normal"
      data-testid={testId}
    >
      {shown.map(p => (
        <span key={p.id} role="img" aria-label={nameOf(p)} title={participantTitle(p)} className={`${CHIP_CLS} text-text`}>
          <span aria-hidden>{principalInitials(p)}</span>
        </span>
      ))}
      {more > 0 && (
        <span
          role="img"
          aria-label={i18nT('sharedCrew.participants_more', { n: fmtNumber(more) })}
          title={fmtList([...rest.map(participantTitle), ...(dropped > 0 ? [i18nT('sharedCrew.participants_more', { n: fmtNumber(dropped) })] : [])], { style: 'narrow' })}
          className={`${CHIP_CLS} text-muted`}
          data-testid={`${testId}-more`}
        >
          <span aria-hidden>{`+${fmtNumber(more)}`}</span>
        </span>
      )}
    </span>
  )
}

/** A Bot row's chips: drawn only on a team Crew. The hook runs only for a row
 *  that has someone to show, so a roster with no participants reads nothing. */
export function TeamParticipantChips(props: ChipsProps) {
  if (!props.participants || props.participants.length === 0) return null
  return <TeamGate {...props} />
}

function TeamGate(props: ChipsProps) {
  const team = useTeamAttribution()
  return team ? <ParticipantChips {...props} /> : null
}
