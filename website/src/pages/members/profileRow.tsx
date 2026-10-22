import { forwardRef, type ForwardedRef, type ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { cn } from '../../lib/utils'
import { forwardUiLocation } from '../../uiLocations/uiLocation'
import type { UiLocationId } from '../../uiLocations/descriptors'

/** The icon-tile tones the Profile card's rows and tiles share. */
export const TONE: Record<'accent' | 'ok' | 'warn' | 'info', string> = {
  accent: 'bg-accent-subtle text-accent',
  ok: 'bg-ok-subtle text-ok-text',
  warn: 'bg-warn-subtle text-warn-text',
  info: 'bg-info-subtle text-info-text',
}

/** The frame every row in a Profile-card group shares: the icon tile, the label
 *  column, then whatever sits at the trailing edge (a chevron, a value, a switch). */
const ROW_CLASS = 'flex items-center gap-3 w-full px-3.5 py-3 text-left border-t border-border first:border-t-0'

export function RowIcon({ icon, tone = 'accent' }: { icon: ReactNode; tone?: keyof typeof TONE }) {
  return <span className={cn('w-8 h-8 rounded-[9px] grid place-items-center shrink-0', TONE[tone])} aria-hidden="true">{icon}</span>
}

export function RowText({ label, sub, labelId, subId }: { label: string; sub?: string; labelId?: string; subId?: string }) {
  return (
    <span className="flex-1 min-w-0 leading-tight">
      <span className="block text-[13px] font-semibold truncate" id={labelId}>{label}</span>
      {sub && <span className="block text-[12px] text-muted truncate" id={subId}>{sub}</span>}
    </span>
  )
}

/** A door: a whole-row button that leads somewhere (a pushed page, another
 *  surface). `value` is the row's current reading, muted, before the chevron —
 *  the iOS Settings shape, so a reader sees what is set without opening it. */
export const Row = forwardRef(function Row({ icon, tone = 'accent', label, sub, value, onClick, testId, disabled, 'data-ui-location': uiLocationId, 'aria-label': ariaLabel }: {
  icon: ReactNode
  tone?: keyof typeof TONE
  label: string
  sub?: string
  /** The current value, right-aligned and muted; omitted for a row that only leads somewhere. */
  value?: string
  onClick: () => void
  testId: string
  disabled?: boolean
  /** A registered find_ui location (`{...uiLocation(id)}` spread on the row). */
  'data-ui-location'?: UiLocationId
  /** Set when the accessible name must be the label alone (a find_ui location
   *  reads it); the reading the row shows — its `value` and its `sub` line —
   *  then rides along as the description, so a screen reader still hears it. */
  'aria-label'?: string
}, forwardedRef: ForwardedRef<HTMLButtonElement>) {
  const valueId = ariaLabel && value ? `${testId}-value` : undefined
  const subId = ariaLabel && sub ? `${testId}-sub` : undefined
  const describedBy = [valueId, subId].filter(Boolean).join(' ') || undefined
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      aria-describedby={describedBy}
      className={cn(ROW_CLASS, 'hover:bg-bg-hover transition-colors cursor-pointer disabled:cursor-default disabled:hover:bg-transparent')}
      data-testid={testId}
      {...forwardUiLocation(uiLocationId, forwardedRef)}
    >
      <RowIcon icon={icon} tone={tone} />
      <RowText label={label} sub={sub} subId={subId} />
      {value && <span className="text-[12.5px] text-muted shrink-0 max-w-[45%] truncate" id={valueId} data-testid={`${testId}-value`}>{value}</span>}
      <ChevronRight size={15} className="text-muted shrink-0" aria-hidden="true" />
    </button>
  )
})

/** A readout row: the same frame, not a button, with its own control at the
 *  trailing edge (a switch). The control carries its own accessible name. */
export function RowStatic({ icon, tone = 'accent', label, sub, children, testId }: {
  icon: ReactNode
  tone?: keyof typeof TONE
  label: string
  sub?: string
  children?: ReactNode
  testId: string
}) {
  return (
    <div className={ROW_CLASS} data-testid={testId}>
      <RowIcon icon={icon} tone={tone} />
      <RowText label={label} sub={sub} />
      {children}
    </div>
  )
}

/** The rounded group the rows sit in, with an optional caption under it (the
 *  iOS Settings footer: one line saying what the group's control does). */
export function RowGroup({ children, caption, testId, className }: { children: ReactNode; caption?: ReactNode; testId?: string; className?: string }) {
  return (
    <div data-testid={testId} className={className}>
      <div className="rounded-2xl border border-border bg-bg overflow-hidden">{children}</div>
      {caption && <div className="px-3.5 pt-1.5 text-[12px] leading-snug text-muted">{caption}</div>}
    </div>
  )
}
