/**
 * The phone list that the Recovery and Failures tabs share: what the row is about,
 * then one line of what is known, ending in the row's one action. A missing value
 * is left out of that line, never printed as a placeholder.
 */
import type { ReactNode } from 'react'
import { useContainerWidth } from '../../../hooks/useContainerWidth'
import { useIsMobile } from '../../../hooks/useIsMobile'
import type { LucideIcon } from 'lucide-react'
import { i18nT } from '../../../i18n/t'

export type MemoryRow = {
  id: string
  /** What the row is about: the memory, or the failing tool. */
  title: ReactNode
  /** A state worth seeing at a glance, beside the title. */
  badge?: ReactNode
  /** More of the row's own text, under the title. */
  body?: ReactNode
  /** The one thing to do with this row, at the end of its meta line. */
  action?: { label: string; icon: LucideIcon; onClick: () => void; disabled?: boolean }
  /** Known facts, joined into one line; empty entries drop out. */
  meta: ReactNode[]
  /** A status line for this row (a save result). */
  status?: ReactNode
  testId?: string
}

/** Below this card width a table of long text reads worse than the list. */
const LIST_BELOW_PX = 900

/** The list layout on a phone or in a card narrower than a table needs; the ref goes on the card's content. */
export function useMemoryRowLayout(): [React.RefCallback<HTMLDivElement>, boolean] {
  const phone = useIsMobile()
  const [measure, width] = useContainerWidth<HTMLDivElement>()
  return [measure, phone || (width !== null && width < LIST_BELOW_PX)]
}

/** "Label: value", punctuated the way each language writes it. */
export const fieldValue = (label: string, value: string) => i18nT('memoryPage.field_value', { label, value })

export function MemoryRowList({ rows }: { rows: MemoryRow[] }) {
  return <ul className="divide-y divide-border">{rows.map(row => {
    const meta = row.meta.filter(item => item !== null && item !== undefined && item !== false && item !== '')
    const Icon = row.action?.icon
    return <li key={row.id} className="space-y-1.5 py-3" data-testid={row.testId}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 whitespace-pre-wrap break-words text-[15px] leading-[22px] [overflow-wrap:anywhere]">{row.title}</div>
        {row.badge && <span className="shrink-0">{row.badge}</span>}
      </div>
      {row.body}
      {row.status}
      {/* A long meta line takes the full width and moves the action under it, still at the right. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <p className="min-w-0 max-w-full text-[12px] text-muted [overflow-wrap:anywhere]">{meta.flatMap((item, i) => i ? [' · ', item] : [item])}</p>
        {/* A text action keeps the reading order; the negative margin keeps a 44 px target without growing the row. */}
        {row.action && Icon && <button type="button" disabled={row.action.disabled} onClick={row.action.onClick} className="-my-3 -mr-2 ml-auto inline-flex min-h-11 shrink-0 items-center gap-1 rounded-md px-2 text-[13px] font-medium text-accent focus-ring disabled:opacity-40">
          <Icon className="lucide-inline" aria-hidden="true" />{row.action.label}
        </button>}
      </div>
    </li>
  })}</ul>
}
