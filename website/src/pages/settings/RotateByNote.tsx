import { AlertTriangle } from 'lucide-react'

import { Badge } from '../../components/ui'
import { useSecondClock } from '../../hooks/useSecondClock'
import { fmtDate } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { localDay, parseLocalDay } from '../telemetryRange'

/** The viewer's local day at *nowMs*, as `YYYY-MM-DD`, the shape the vault stores. */
function localDayAt(nowMs: number): string {
  return localDay(new Date(nowMs))
}

/** Whether a rotate-by date has passed: the day after it has begun. Both are
 *  `YYYY-MM-DD`, so they compare as strings. */
export function rotationDue(rotateBy: string, today: string): boolean {
  return today > rotateBy
}

/** A stored day as the viewer's language writes it, with the month name (the way
 *  the pane's "Updated" line does); text that is not a calendar day is shown as
 *  stored. */
function shownDate(rotateBy: string): string {
  const parsed = parseLocalDay(rotateBy)
  return parsed ? fmtDate(parsed) : rotateBy
}

/**
 * The "Rotation due" chip for an entry whose rotate-by date has passed, and
 * nothing before that. It re-reads only when the local day changes, so a chip
 * due at midnight appears on an open page; the hook also catches up when a
 * hidden tab is shown again. `inRow` draws it as a rail row's second line: the
 * row is one button, which reads its own text, so the chip carries no label of
 * its own there.
 */
export function RotationDueChip({ name, rotateBy, inRow = false }: { name: string; rotateBy?: string; inRow?: boolean }) {
  const today = useSecondClock(localDayAt, Boolean(rotateBy))
  if (!rotateBy || !rotationDue(rotateBy, today)) return null
  const chip = (
    <Badge
      variant="warn"
      role={inRow ? undefined : 'note'}
      aria-label={inRow ? undefined : i18nT('settings.secrets.rotation_due_aria', { name, date: shownDate(rotateBy) })}
    >
      <AlertTriangle className="lucide-inline" aria-hidden />
      {i18nT('settings.secrets.rotation_due')}
    </Badge>
  )
  return inRow ? <span className="mt-0.5 flex">{chip}</span> : chip
}

/**
 * "Rotate by <date>" for a vault entry that carries one, and a "Rotation due"
 * chip once that date has passed. `preview` is the form's note under the date
 * field: the text only, so a typed date is not read as the entry's saved status.
 * Renders nothing for an entry with no date, so the entry reads exactly as it did
 * before the field existed.
 */
export function RotateByNote({ name, rotateBy, preview = false }: { name: string; rotateBy?: string; preview?: boolean }) {
  if (!rotateBy) return null
  return (
    <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px] text-muted">
      <span>{i18nT('settings.secrets.rotate_by_note', { date: shownDate(rotateBy) })}</span>
      {!preview && <RotationDueChip name={name} rotateBy={rotateBy} />}
    </div>
  )
}
