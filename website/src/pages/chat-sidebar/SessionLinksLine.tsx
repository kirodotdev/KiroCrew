import { useState, type ReactNode } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Link2 } from 'lucide-react'
import SourceProviderMark from '../../components/SourceProviderMark'
import { i18nT } from '../../i18n/t'
import { ROW_META_CLS } from '../../components/listShell'
import { SessionSourceChips, showsChipCi } from './SessionSourceChips'
import type { SidebarSourceLink } from './types'

type SourceChipsProps = Parameters<typeof SessionSourceChips>[0]

/**
 * A session row's third line: its status (or last message) on the left, and the
 * count of everything it links to on the right (#38200). The count is the row's
 * only link marker, so the row stays three lines tall; pressing it expands the
 * full chip strip, the same `SessionSourceChips` every row used to draw, under
 * the line, and pressing it again folds it. Each row folds on its own.
 *
 * The expanded strip sits on a 2px top pad so an expanded row grows by 20px per
 * chip line and stays on the row's 4px grid (the chip line itself is 18px).
 */
export function SessionStatusLinksLine({ status, links, total, ...chips }: {
  /** The line's left side: the status resolver's output. */
  status: ReactNode
  links: SidebarSourceLink[] | undefined
  total?: number
} & Omit<SourceChipsProps, 'links' | 'total'>) {
  const [open, setOpen] = useState(false)
  if (!links || links.length === 0) return <>{status}</>
  return (
    <>
      <div className="flex items-center gap-2 min-w-0" data-testid="session-status-links-line">
        <div className="flex-1 min-w-0">{status}</div>
        <SessionLinksCount links={links} total={total} expanded={open} onToggle={() => setOpen(o => !o)} />
      </div>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div key="links" data-testid="session-links-expanded" className="pt-0.5 overflow-hidden"
            initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}>
            <SessionSourceChips links={links} total={total} {...chips} />
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )
}

/** The count itself: the provider's mark when every link shares one provider, a
 *  plain link mark when they are mixed, and a red dot only when a check failed.
 *  No arrow: the pressed box says it is open, and the strip below says what. */
export function SessionLinksCount({ links, total, expanded, onToggle }: {
  links: SidebarSourceLink[]
  total?: number
  expanded: boolean
  onToggle: () => void
}) {
  const shown = Math.max(total ?? 0, links.length)
  // The chips' own gate: a merged or closed source's CI is not shown, so its
  // old failure never marks the count either.
  const failed = links.some(link => link.ci === 'failed' && showsChipCi(link.state))
  // The payload carries at most three links; a provider logo for a partial
  // list could hide a different provider among the rest.
  const oneProvider = (total === undefined || total <= links.length)
    && new Set(links.map(link => link.provider)).size === 1
  // The dot is drawn, not read, so a failure is named in the label as well.
  const label = expanded
    ? i18nT('pages.chatSidebar.hide_linked_items')
    : failed
      ? i18nT('pages.chatSidebar.show_linked_items_failed', { count: shown })
      : i18nT('pages.chatSidebar.show_linked_items', { count: shown })
  return (
    <button type="button" data-testid="session-links-count" aria-expanded={expanded} aria-label={label} title={label}
      // The row's own click opens the session; this press only folds the strip.
      onMouseDown={e => { e.stopPropagation(); e.preventDefault() }}
      onClick={e => {
        e.stopPropagation()
        onToggle()
        // A tap focuses the button, and the row's clipped content column scrolls
        // sideways to show it; put the column back where every other row has it.
        for (let el = e.currentTarget.parentElement; el && !el.classList.contains('session-row'); el = el.parentElement) el.scrollLeft = 0
      }}
      className={`${ROW_META_CLS} shrink-0 inline-flex items-center gap-1 px-1 rounded border-none font-normal tabular-nums cursor-pointer ${expanded ? 'bg-bg-hover text-text' : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover'}`}>
      {oneProvider ? <SourceProviderMark provider={links[0].provider} size={10} /> : <Link2 size={10} aria-hidden="true" />}
      {shown}
      {failed && <span aria-hidden="true" data-testid="session-links-failed" className="w-1.5 h-1.5 rounded-full bg-[var(--danger)]" />}
    </button>
  )
}
