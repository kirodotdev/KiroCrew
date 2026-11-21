/**
 * The "Idle timeout…" dialog on a managed environment's crew row: a whole number in minutes,
 * hours or days, 15 minutes to 7 days, with the presets as quick picks, and what a change does
 * in the environment's state. It only asks; SandboxCrewRowDetail posts
 * `POST /api/managed-environments/{id}/idle-timeout`, whose server checks the range again.
 *
 * - RUNNING: the change restarts the environment, so the warning is shown and the button says so.
 * - STOPPED: the value is kept for the next resume.
 * - Any other state, or another action running on it: no Save, with the busy text.
 */
import { useEffect, useState } from 'react'
import { Btn } from '../ui'
import Modal from '../Modal'
import SimpleSelect from '../SimpleSelect'
import type { SandboxItem } from '../../api/managedEnvApi'
import { IDLE_PRESET_MINUTES, IDLE_TEXT, idleDialogTitle, idleSpanText, idleStopsAfter, idleUnitLabel } from './text'

export const IDLE_MIN = 15
export const IDLE_MAX = 10080

/** The field takes a number in a unit, and the presets are only quick picks. */
export type IdleUnit = 'minutes' | 'hours' | 'days'
export const IDLE_UNIT_MINUTES: Record<IdleUnit, number> = { minutes: 1, hours: 60, days: 1440 }
export const IDLE_UNITS = Object.keys(IDLE_UNIT_MINUTES) as IdleUnit[]

/** The minutes for a whole number in a unit, or null outside 15 to 10080 minutes. */
export function idleMinutes(text: string, unit: IdleUnit = 'minutes'): number | null {
  if (!/^\d+$/.test(text.trim())) return null
  const n = Number(text.trim()) * IDLE_UNIT_MINUTES[unit]
  return n >= IDLE_MIN && n <= IDLE_MAX ? n : null
}

/** The row's line about it: the stored value "where idle stop is on", or the switch's own answer. */
export function idleLine(item: SandboxItem, idleOff: boolean): string {
  if (idleOff) return IDLE_TEXT.off
  const m = item.idle_timeout_minutes
  if (typeof m !== 'number') return ''
  return idleStopsAfter(m)
}

export default function SandboxIdleTimeoutDialog({
  item,
  crewName,
  open,
  busy,
  onClose,
  onSave,
}: {
  item: SandboxItem
  crewName: string
  open: boolean
  busy: boolean
  onClose: () => void
  onSave: (minutes: number) => void
}) {
  const [text, setText] = useState(String(item.idle_timeout_minutes ?? 60))
  const [unit, setUnit] = useState<IdleUnit>('minutes')
  useEffect(() => {
    if (open) {
      setText(String(item.idle_timeout_minutes ?? 60))
      setUnit('minutes')
    }
  }, [open, item.idle_timeout_minutes])
  const minutes = idleMinutes(text, unit)
  // An action that starts while the dialog is open (the list polls) takes Save away: the
  // environment is busy, and the route would refuse the change with a 409.
  const actionRunning = Boolean(item.action?.running)
  const running = item.status === 'RUNNING' && !actionRunning
  const stopped = item.status === 'STOPPED' && !actionRunning
  const canSave = (running || stopped) && minutes !== null && !busy
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={idleDialogTitle(crewName)}
      maxWidth={440}
      footer={
        <>
          <Btn onClick={onClose}>{IDLE_TEXT.cancel}</Btn>
          <Btn
            primary={!running}
            danger={running}
            disabled={!canSave}
            onClick={() => minutes !== null && onSave(minutes)}
            data-testid={`sandbox-idle-save-${item.sandbox_id}`}
          >
            {running ? IDLE_TEXT.saveRestart : IDLE_TEXT.save}
          </Btn>
        </>
      }
    >
      <div className="space-y-3" data-testid={`sandbox-idle-dialog-${item.sandbox_id}`}>
        <label className="block">
          <span className="text-[12px] font-medium text-muted">{IDLE_TEXT.field}</span>
          <span className="mt-1 flex gap-2">
            <input
              value={text}
              onChange={e => setText(e.target.value)}
              inputMode="numeric"
              aria-invalid={minutes === null}
              className="w-full font-mono"
              aria-label={IDLE_TEXT.field}
              data-testid={`sandbox-idle-minutes-${item.sandbox_id}`}
            />
            <span data-testid={`sandbox-idle-unit-${item.sandbox_id}`}>
              <SimpleSelect
                options={IDLE_UNITS}
                optionLabels={IDLE_UNITS.map(u => idleUnitLabel(u))}
                value={unit}
                onChange={next => setUnit(next as IdleUnit)}
                aria-label={IDLE_TEXT.unit}
              />
            </span>
          </span>
        </label>
        <p className="text-[12px] text-muted" data-testid={`sandbox-idle-hint-${item.sandbox_id}`}>
          {IDLE_TEXT.hint}
        </p>
        <div className="flex flex-wrap gap-2">
          {IDLE_PRESET_MINUTES.map(value => (
            <Btn
              key={value}
              onClick={() => {
                // a quick pick fills the field in its own unit: "4 hours" is 4 in hours
                const [n, u] = value % 1440 === 0 ? [value / 1440, 'days' as const] : [value / 60, 'hours' as const]
                setText(String(n))
                setUnit(u)
              }}
              data-testid={`sandbox-idle-preset-${value}`}
            >
              {idleSpanText(value)}
            </Btn>
          ))}
        </div>
        {minutes === null ? (
          <p className="text-[12px] text-danger" data-testid={`sandbox-idle-refused-${item.sandbox_id}`}>
            {IDLE_TEXT.range}
          </p>
        ) : null}
        <p className="text-[12px] text-text" data-testid={`sandbox-idle-effect-${item.sandbox_id}`}>
          {running ? IDLE_TEXT.running : stopped ? IDLE_TEXT.stopped : IDLE_TEXT.busy}
        </p>
      </div>
    </Modal>
  )
}
