/** Who else is in this session right now: one initials chip per person,
 *  however many tabs they have open (no avatar is ever fetched), the viewer left
 *  out. A chip's name is its accessible name and hover title; no text is drawn
 *  beside it, and past three people the rest fold into "+N". It lives in the
 *  composer's context row and shows only on a team Crew (`useTeamAttribution`),
 *  so a solo session draws nothing. Who is typing is not said here: the locked
 *  composer says it (`TypingLockBanner`). */
import type { PresenceEntry, PublicPrincipal } from '../../../api/sharedCrew'
import { capabilityUsable, principalInitials, principalName } from '../../../api/sharedCrewUi'
import {
  useSessionPresence,
  useSharedCrewCapabilities,
  useSharedCrewViewer,
  useTeamAttribution,
} from '../../../hooks/sharedCrew/useSharedCrew'
import { fmtNumber } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'

/** Visible chips before the rest fold into "+N". */
const MAX_CHIPS = 3

/** The presence entries the chips show: other people, not idle. */
export function othersPresent(entries: readonly PresenceEntry[], viewer: PublicPrincipal | null | undefined): PresenceEntry[] {
  return entries.filter(e => e.principal.id !== viewer?.id && e.activity !== 'idle')
}

export function PresenceChips({ entries, viewer }: { entries: readonly PresenceEntry[]; viewer: PublicPrincipal | null | undefined }) {
  const others = othersPresent(entries, viewer)
  if (others.length === 0) return null
  const shown = others.slice(0, MAX_CHIPS)
  const rest = others.length - shown.length
  const hidden = others.slice(shown.length).map(e => principalName(e.principal) || i18nT('sharedCrew.unattributed')).join(', ')
  return (
    <ul className="flex items-center gap-0.5 shrink-0" aria-label={i18nT('sharedCrew.here_now', { n: fmtNumber(others.length) })} data-testid="presence-chips">
      {shown.map(e => {
        const label = principalName(e.principal) || i18nT('sharedCrew.unattributed')
        return (
          <li
            key={e.principal.id}
            title={label}
            aria-label={label}
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard users reach the name tooltip.
            tabIndex={0}
            className="inline-flex items-center justify-center w-5 h-5 rounded-full text-[10px] font-semibold border border-bg bg-bg-hover text-text"
          >
            <span aria-hidden>{principalInitials(e.principal)}</span>
          </li>
        )
      })}
      {rest > 0 && (
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard users reach the hidden names.
        <li className="inline-flex items-center justify-center h-5 px-1 rounded-full text-[10px] text-muted border border-bg bg-bg-hover" tabIndex={0} title={hidden} aria-label={i18nT('sharedCrew.more_people', { n: fmtNumber(rest) })}>
          <span aria-hidden>{`+${fmtNumber(rest)}`}</span>
        </li>
      )}
    </ul>
  )
}

/** The other people in the open session, read from the cache `SessionCollabStrip`
 *  keeps warm. Empty on a solo or non-team Crew, and until the viewer read
 *  settles: the viewer's own entry cannot be told apart from another person's
 *  before then, and a chip that flashed for the viewer and then went would shift
 *  the row under the pointer. */
export function usePresentOthers(sessionKey: string | null | undefined) {
  const { capabilities } = useSharedCrewCapabilities()
  const presenceOn = capabilityUsable(capabilities.presence)
  const viewer = useSharedCrewViewer(presenceOn || capabilityUsable(capabilities.attribution))
  const presence = useSessionPresence(sessionKey, presenceOn)
  const team = useTeamAttribution()
  const entries = presence.data?.entries ?? []
  if (!team || viewer.isPending) return { entries: [], viewer: null }
  return { entries: othersPresent(entries, viewer.data), viewer: viewer.data }
}

/** The open session's presence chips. */
export function SessionPresence({ sessionKey }: { sessionKey: string | null | undefined }) {
  const { entries, viewer } = usePresentOthers(sessionKey)
  return <PresenceChips entries={entries} viewer={viewer} />
}
