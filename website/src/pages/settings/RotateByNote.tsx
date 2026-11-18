import { AlertTriangle } from 'lucide-react'

import { Badge } from '../../components/ui'
import { useSecondClock } from '../../hooks/useSecondClock'
import { fmtDateNumeric } from '../../i18n/format'
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

/**
 * "Rotate by <date>" under a vault entry that carries one, and a "Rotation due"
 * chip once that date has passed. Renders nothing for an entry with no date, so
 * the row reads exactly as it did before the field existed.
 */
export function RotateByNote({ name, rotateBy }: { name: string; rotateBy?: string }) {
  // Re-read only when the local day changes, so a chip due at midnight appears
  // on an open page; the hook also catches up when a hidden tab is shown again.
  const today = useSecondClock(localDayAt, Boolean(rotateBy))
  if (!rotateBy) return null
  const due = rotationDue(rotateBy, today)
  const parsed = parseLocalDay(rotateBy)
  const date = parsed ? fmtDateNumeric(parsed) : rotateBy
  return (
    <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px] text-muted">
      <span>{i18nT('settings.secrets.rotate_by_note', { date })}</span>
      {due && (
        <Badge
          variant="warn"
          role="note"
          aria-label={i18nT('settings.secrets.rotation_due_aria', { name, date })}
        >
          <AlertTriangle className="lucide-inline" aria-hidden />
          {i18nT('settings.secrets.rotation_due')}
        </Badge>
      )}
    </div>
  )
}
