import { Glass } from './Glass'
import { KiroGhostMark } from './KiroGhostMark'

import { i18nT } from '../i18n/t'

interface CrewModeChipProps {
  /** Turns this chat into a Crew Mode chat. The composer's mark turns it off
   *  again until the first message is sent. */
  onTurnOn: () => void
  /** Greyed out while the chip cannot be pressed (a memory-mode switch). */
  disabled?: boolean
  /** A reply is running: the agent switch refuses mid-turn. Only the cursor and
   *  hint change. It stays focusable (`aria-disabled`, not `disabled`), so a
   *  keyboard reaches it and hears its described-by hint. */
  busy?: boolean
  /** Id of a visible line that says more about the chip. */
  describedBy?: string
}

/** The composer's Crew Mode switch, to the right of the memory-mode chip on a new
 *  chat. Same glass chip primitive as `MemoryModeChip`. Its accessible name is
 *  always "Crew Mode" and `aria-pressed` stays false: pressing it turns the chat
 *  into a Crew Mode chat and the chip moves into the composer's mark, which is
 *  the pressed toggle until the first message. */
export function CrewModeChip({ onTurnOn, disabled = false, busy = false, describedBy }: CrewModeChipProps) {
  const hint = busy ? i18nT('components.crewMode.chip_hint_busy') : i18nT('components.crewMode.chip_hint_off')
  // Busy reads like disabled (dimmed, plain border: `.crew-chip[aria-disabled]`
  // in crew-mode.css drops the accent), so it is never mistaken for pressed.
  const state = busy
    ? 'aria-disabled:cursor-not-allowed aria-disabled:opacity-60'
    : 'disabled:cursor-default disabled:opacity-60'
  return (
    <Glass
      as="button"
      variant="chip"
      radius={8}
      type="button"
      data-testid="crew-mode-chip"
      aria-label={i18nT('components.crewMode.chip_off')}
      aria-pressed={false}
      aria-describedby={describedBy}
      aria-disabled={busy || undefined}
      title={hint}
      disabled={disabled}
      onClick={busy ? undefined : onTurnOn}
      className={`crew-chip inline-flex items-center gap-1.5 rounded-lg border px-3 py-1 text-[12px] font-semibold transition-colors cursor-pointer ${state}`}
    >
      <KiroGhostMark size={13} className="inline-block shrink-0 crew-tone-aim" />
      {/* Just the name; what it does is in the tooltip. */}
      <span className="crew-chip-text">{i18nT('components.crewMode.chip_off')}</span>
    </Glass>
  )
}
