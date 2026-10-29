/** Who else is in this session right now. One chip per person, however many
 *  tabs they have open; initials only (no avatar is ever fetched); the viewer
 *  is left out. Typing is a word, not an animation; the composer's typing
 *  lease (useComposerTypingLock) is what locks the box. */
import type { PresenceEntry, PublicPrincipal } from '../../../api/sharedCrew'
import { principalInitials, principalName } from '../../../api/sharedCrewUi'
import { fmtNumber } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'

/** Visible chips before the rest fold into "+N". */
const MAX_CHIPS = 4

export function PresenceChips({ entries, viewer }: { entries: readonly PresenceEntry[]; viewer: PublicPrincipal | null | undefined }) {
  const others = entries.filter(e => e.principal.id !== viewer?.id && e.activity !== 'idle')
  if (others.length === 0) return null
  const shown = others.slice(0, MAX_CHIPS)
  const rest = others.length - shown.length
  const typing = others.filter(e => e.activity === 'typing')
  const typingName = typing.length === 1 ? (principalName(typing[0].principal) || i18nT('sharedCrew.unattributed')) : ''
  return (
    <div className="flex items-center gap-1.5 min-w-0" data-testid="presence-chips">
      <ul className="flex items-center -space-x-1 shrink-0" aria-label={i18nT('sharedCrew.here_now')}>
        {shown.map(e => {
          const name = principalName(e.principal) || i18nT('sharedCrew.unattributed')
          const label = e.activity === 'typing' ? i18nT('sharedCrew.person_typing', { name }) : i18nT('sharedCrew.person_viewing', { name })
          return (
            <li
              key={e.principal.id}
              title={label}
              aria-label={label}
              className={`inline-flex items-center justify-center w-5 h-5 rounded-full text-[10px] font-semibold border border-bg bg-bg-hover text-text ${e.activity === 'typing' ? 'ring-1 ring-accent' : ''}`}
            >
              <span aria-hidden>{principalInitials(e.principal)}</span>
            </li>
          )
        })}
        {rest > 0 && (
          <li className="inline-flex items-center justify-center h-5 px-1 rounded-full text-[10px] text-muted border border-bg bg-bg-hover" aria-label={i18nT('sharedCrew.more_people', { n: fmtNumber(rest) })}>
            <span aria-hidden>{`+${fmtNumber(rest)}`}</span>
          </li>
        )}
      </ul>
      <span className="text-[12px] text-muted truncate" aria-live="polite" data-testid="presence-typing">
        {typing.length === 1 ? i18nT('sharedCrew.person_typing', { name: typingName })
          : typing.length > 1 ? i18nT('sharedCrew.several_typing')
            : ''}
      </span>
    </div>
  )
}
