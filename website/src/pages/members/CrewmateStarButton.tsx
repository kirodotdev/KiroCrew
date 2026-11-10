/**
 * The star on a crewmate row, drawn by both lists of the one roster: the full
 * roster's `MemberRow` (MembersPageRosterRows.tsx) and the switcher's option
 * (CrewmateSwitcher.tsx). The host owns the write (`toggleStar` in
 * MembersPageRoster.tsx: one optimistic flip, one pending lock, one failure
 * notice), so a star pressed in either list is the same star.
 *
 * It is a SIBLING of the row's own button, never a child: a button inside a
 * button is invalid HTML and breaks keyboard activation. The host places it
 * over the row's right padding inside a `group/row relative` wrapper.
 *
 * Always shown when starred. Unstarred: shown below md (touch has no hover or
 * keyboard focus to reveal it), revealed by hover or focus at md+ so a desktop
 * list stays quiet. Never hidden from assistive tech: opacity, not display.
 */
import { Star } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import { cn } from '../../lib/utils'

export default function CrewmateStarButton({
  m,
  onToggle,
  pending,
  testId,
}: {
  /** The row's record, the projected view when the host has one. */
  m: MemberRosterRow
  onToggle: (m: MemberRosterRow) => void
  /** A star write for this row is in flight: the control holds until it lands. */
  pending: boolean
  testId: string
}) {
  const { t } = useTranslation()
  const label = t(m.starred ? 'pages.membersPage.unstar' : 'pages.membersPage.star', { name: crewDisplayName(m) })
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation()
        if (!pending) onToggle(m)
      }}
      aria-pressed={!!m.starred}
      // Held, never `disabled`, while the write is in flight: disabling the
      // star a reader just pressed blurs it to <body>, and an unstarred star at
      // md+ is shown only while hovered or focused, so it would vanish too.
      aria-disabled={pending || undefined}
      aria-label={label}
      title={label}
      // 24x24 minimum target (the icon is 13px): a touch that lands beside
      // the glyph must hit the star, not the row button underneath.
      className={cn(
        'absolute right-1 top-1/2 -translate-y-1/2 flex items-center justify-center w-6 h-6 rounded hover:bg-bg-hover transition-opacity',
        m.starred
          ? 'opacity-100 text-accent'
          : 'md:opacity-0 md:group-hover/row:opacity-100 [@media(hover:none)]:opacity-100 md:focus-visible:opacity-100 text-muted',
      )}
      data-testid={testId}
    >
      <Star size={13} {...(m.starred ? { fill: 'var(--accent)', stroke: 'none' } : {})} />
    </button>
  )
}
