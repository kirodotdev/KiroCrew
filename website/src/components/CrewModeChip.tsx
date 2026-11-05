import { useRef } from 'react'
import { Glass } from './Glass'
import { Zap } from 'lucide-react'

import { i18nT } from '../i18n/t'
import { fireUltraEffect } from '../lib/ultraModeEffect'

interface CrewModeChipProps {
  /** Turns this chat into an Ultra Mode chat. */
  onTurnOn: () => void
  /** the chip is a toggle. While Ultra Mode is on and no message is
   *  sent yet it shows pressed, and a click turns it off again. */
  pressed?: boolean
  onTurnOff?: () => void
  /** Greyed out while the chip cannot be pressed (a memory-mode switch, or a
   *  switch in flight). */
  disabled?: boolean
  /** A reply is running: the agent switch refuses mid-turn. */
  busy?: boolean
  /** Id of a visible line that says more about the chip. */
  describedBy?: string
  /** The mid-chat switch (right of the permission button) has its own id. */
  testId?: string
}

/** The composer's Ultra Mode switch. On a NEW chat (before its first message)
 *  it toggles in place -- pressed is on -- and the first message locks it. In a
 *  plain chat that already has messages the same chip, unpressed, sits right of
 *  the permission button and turns the chat on one way. */
export function CrewModeChip({ onTurnOn, pressed = false, onTurnOff, disabled = false, busy = false, describedBy, testId = 'crew-mode-chip' }: CrewModeChipProps) {
  const ref = useRef<HTMLElement | null>(null)
  const hint = busy
    ? i18nT('components.crewMode.chip_hint_busy')
    : pressed ? i18nT('components.crewMode.chip_hint_pressed') : i18nT('components.crewMode.chip_hint_off')
  const state = busy
    ? 'aria-disabled:cursor-not-allowed aria-disabled:opacity-60'
    : 'disabled:cursor-default disabled:opacity-60'
  const click = () => {
    if (busy) return
    if (pressed) { onTurnOff?.(); return }
    fireUltraEffect(ref.current?.getBoundingClientRect() ?? null)
    onTurnOn()
  }
  return (
    <Glass
      as="button"
      variant="chip"
      radius={8}
      type="button"
      ref={ref as never}
      data-testid={testId}
      aria-label={i18nT('components.crewMode.chip_off')}
      aria-pressed={pressed}
      aria-describedby={describedBy}
      aria-disabled={busy || undefined}
      title={hint}
      disabled={disabled || (pressed && !onTurnOff)}
      onClick={click}
      className={`crew-chip inline-flex items-center gap-1.5 rounded-lg border px-3 py-1 text-[12px] font-semibold transition-colors cursor-pointer ${state}`}
    >
      <Zap size={13} aria-hidden="true" fill={pressed ? 'currentColor' : 'none'} className="inline-block shrink-0 ultra-accent" />
      <span className="crew-chip-text">{i18nT('components.crewMode.chip_off')}</span>
    </Glass>
  )
}
