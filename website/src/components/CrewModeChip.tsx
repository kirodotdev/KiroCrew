import { X } from 'lucide-react'
import { Glass } from './Glass'
import { KiroGhostMark } from './KiroGhostMark'

import { i18nT } from '../i18n/t'

interface CrewModeChipProps {
  on: boolean
  onToggle: () => void
  /** Greyed out while the chip cannot be flipped (a memory-mode switch). */
  disabled?: boolean
  /** A reply is running: the agent switch refuses mid-turn. The chip keeps its
   *  look so an on chip still reads as on; only the cursor and hint change. */
  busy?: boolean
  /** Id of a visible line that says more about the chip. */
  describedBy?: string
}

/** The composer's Crew Mode switch, sitting to the right of the memory-mode chip.
 *  Same glass chip primitive as `MemoryModeChip`; `aria-pressed` carries the state. */
export function CrewModeChip({ on, onToggle, disabled = false, busy = false, describedBy }: CrewModeChipProps) {
  const hint = busy
    ? i18nT('components.crewMode.chip_hint_busy')
    : on ? i18nT('components.crewMode.chip_hint_on') : i18nT('components.crewMode.chip_hint_off')
  const state = busy
    ? 'disabled:cursor-not-allowed'
    : 'disabled:cursor-default disabled:opacity-60'
  return (
    <Glass
      as="button"
      variant="chip"
      radius={8}
      type="button"
      data-testid="crew-mode-chip"
      aria-pressed={on}
      aria-describedby={describedBy}
      title={hint}
      disabled={disabled || busy}
      onClick={onToggle}
      className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1 text-[12px] transition-colors cursor-pointer ${state} ${
        on
          ? 'border-transparent glass-accent text-aim font-semibold'
          : 'border-transparent glass-hover text-muted hover:text-text'
      }`}
    >
      <KiroGhostMark size={13} />
      {on ? (
        <span>{i18nT('components.crewMode.chip_on')}</span>
      ) : (
        <>
          {/* Off, the label says what turning it on does; a phone keeps the
              short name so the chip row stays on one line. */}
          <span className="sm:hidden">{i18nT('components.crewMode.chip_off')}</span>
          <span className="hidden sm:inline">{i18nT('components.crewMode.chip_off_explained')}</span>
        </>
      )}
      {/* A visible "off" mark, so a reader who meets the chip mid-chat can see
          that clicking it turns the mode off. */}
      {on && <X size={12} aria-hidden="true" />}
    </Glass>
  )
}
