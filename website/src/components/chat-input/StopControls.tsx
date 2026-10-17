import { motion } from 'framer-motion'
import { Loader2, Square } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'

/** The Stop control through a stop's phases: the armed Stop, the pulsing
 *  force-stop while a soft cancel is pending, and the hard kill (a spinner,
 *  then a "force reset" escape hatch once the kill stalls). Shared by the
 *  dashboard composer (`BusySendControls`) and the app SDK's `ChatEmbed`, so
 *  both surfaces draw one control. Store-free on purpose: the embed carries
 *  no Redux. `inlineHints={false}` lets a host that has no room beside the
 *  button render the force-stop and escape-hatch hints elsewhere. The armed Stop carries the
 *  composer's `composer.stop` find_ui marker unless `guideTarget={false}`. */
export function StopControls({ stopState, killingEscaped, stopWithTap, terminalActive = false, inlineHints = true, guideTarget = true }: {
  stopState?: 'idle' | 'soft_pending' | 'killing'
  killingEscaped: boolean
  stopWithTap: () => void
  terminalActive?: boolean
  inlineHints?: boolean
  /** Whether the armed Stop is the guide's `composer.stop` target. That
   *  location lives on the chat page, so an embed inside an app passes
   *  `false`: otherwise every running embed would register a second target
   *  and the guide could point into an app. */
  guideTarget?: boolean
}) {
  if (stopState === 'killing') {
    return killingEscaped ? (
      <div className={`flex items-center gap-1.5${terminalActive ? ' min-w-0 [&>button]:shrink-0' : ''}`}>
        <button
          className="w-8 h-8 rounded-lg bg-danger text-danger-fg border-none flex items-center justify-center cursor-pointer hover:bg-danger/80 transition-all"
          onClick={stopWithTap}
          title={i18nT('components.chatInput.force_reset_taking_longer_than_expected')}
          aria-label={i18nT('components.chatInput.force_reset_session_taking_longer_than_expected')}
          data-testid="stop-button-escape-hatch"
        >
          <Square size={18} fill="currentColor" />
        </button>
        {inlineHints && <span className={`text-xs text-muted${terminalActive ? ' min-w-0 whitespace-normal [overflow-wrap:anywhere]' : ' whitespace-nowrap'}`} data-testid="stop-escape-hint">{i18nT('components.chatInput.taking_longer_than_expected')}</span>}
      </div>
    ) : (
      <button className="w-8 h-8 rounded-lg bg-danger text-danger-fg border-none flex items-center justify-center cursor-not-allowed transition-all" disabled title={i18nT('components.chatInput.killing')} aria-label={i18nT('components.chatInput.killing_session')} data-testid="stop-button-killing">
        <Loader2 size={18} className="animate-spin" />
      </button>
    )
  }
  if (stopState === 'soft_pending') {
    return (
      <div className={`flex items-center gap-1.5${terminalActive ? ' min-w-0 [&>button]:shrink-0' : ''}`}>
        {/* Pulse floor 0.8 with a faint danger fill: at 0.6 on a
            transparent background the light-theme button bottomed
            out near white-on-white mid-pulse, and this is the only
            force-stop path while a cancel hangs (#9548 UX review). */}
        <motion.button
          className="w-8 h-8 rounded-lg bg-danger/10 border-none text-danger-text hover:bg-danger/20 flex items-center justify-center cursor-pointer transition-all"
          onClick={stopWithTap}
          title={i18nT('components.chatInput.force_kill_discards_in_progress_work_and_queued')}
          aria-label={i18nT('components.chatInput.force_kill_session_discards_in_progress_work_and')}
          animate={{ opacity: [0.8, 1, 0.8] }}
          transition={{ duration: 1.2, repeat: Infinity }}
          data-testid="stop-button-pulsing"
        >
          <Square size={18} fill="currentColor" />
        </motion.button>
        {inlineHints && terminalActive && <span aria-hidden="true" className="text-xs text-muted min-w-0 [overflow-wrap:anywhere] @min-[400px]/composer:hidden">{i18nT('components.chatInput.terminal_force_stop')}</span>}
        {inlineHints && <span className={`text-xs text-muted whitespace-nowrap${terminalActive ? ' sr-only @min-[400px]/composer:not-sr-only @min-[400px]/composer:whitespace-normal' : ''}`} data-testid="stop-force-hint">{i18nT('components.chatInput.click_again_to_force_stop')}</span>}
      </div>
    )
  }
  const armed = {
    className: 'w-8 h-8 rounded-lg bg-transparent border-none text-danger hover:text-danger-text hover:bg-danger/10 flex items-center justify-center cursor-pointer transition-all',
    onClick: stopWithTap,
    title: i18nT('components.chatInput.stop_generation'),
    'data-testid': 'stop-button-armed',
  }
  // gen-ui-index wants the marker spread straight onto a JSX element, so the
  // guide target and the plain button are two elements, not one conditional.
  return guideTarget ? (
    <button {...armed} aria-label={i18nT('components.chatInput.stop_generation')} {...uiLocation('composer.stop')}>
      <Square size={18} fill="currentColor" />
    </button>
  ) : (
    <button {...armed} aria-label={i18nT('components.chatInput.stop_generation')}>
      <Square size={18} fill="currentColor" />
    </button>
  )
}
