import { useId } from 'react'

import { InstantTip, useInstantTip } from '../../components/InstantTip'
import { i18nT } from '../../i18n/t'

export type PressurePosture = 'tight' | 'critical'

/**
 * The capsule's "Tight · cap: N" chip, shown while host memory is short.
 *
 * It is a focusable button so a keyboard user can reach its explanation, and
 * its tip opens on a tap so a touch screen (a tablet, a touch laptop) can read
 * it too. The chip has no click action, so a tap that opens the tip loses
 * nothing (`openOnTap`). The capsule is desktop-only, so the chip never
 * renders on a phone.
 *
 * The tip says what the posture means and, when the gateway reports one, what
 * the cap is: how many subagents may run at once right now. The same text is
 * the button's description through a `hidden` span, which stays in the DOM
 * while the bubble is closed. No `title`, so the native tooltip never shows a
 * second copy.
 */
export function ResourcePostureChip({ posture, availableGb, subagentCap, seg }: {
  posture: PressurePosture
  availableGb?: number | null
  subagentCap?: number | null
  /** The capsule's shared segment classes. */
  seg: string
}) {
  const { tip, tipHandlers, tipId } = useInstantTip({ placement: 'below', openOnTap: true })
  const descId = useId()
  // The bubble's id is replaced by the always-present description below.
  const { 'aria-describedby': _bubbleId, ...handlers } = tipHandlers
  void _bubbleId
  const critical = posture === 'critical'
  const gb = availableGb?.toFixed(1) ?? '?'
  const label = critical ? i18nT('app.resource_critical') : i18nT('app.resource_tight')
  const summary = critical
    ? i18nT('app.resource_posture_tooltip_critical', { gb })
    : i18nT('app.resource_posture_tooltip_tight', { gb })
  const capLine = subagentCap != null
    ? i18nT('app.subagent_cap_explained', { count: subagentCap })
    : null
  const tipBody = (
    <>
      <span className="block">{summary}</span>
      {capLine && <span className="block mt-0.5">{capLine}</span>}
    </>
  )

  return (
    <>
      <button
        type="button"
        data-testid="resource-health"
        className={`${seg} flex items-center gap-1 text-[11px] focus-visible:outline-2 focus-visible:outline-accent ${critical ? 'text-danger' : 'text-warn'}`}
        aria-label={label}
        aria-describedby={descId}
        {...handlers}
      >
        <span aria-hidden="true" data-testid="resource-health-dot" className={`inline-block w-2 h-2 rounded-full ${critical ? 'bg-danger animate-pulse [animation-iteration-count:3]! motion-reduce:animate-none' : 'bg-warn'}`} />
        <span aria-hidden="true" className="font-medium">{label}</span>
        {subagentCap != null && (
          <span aria-hidden="true" className="text-muted text-[10px]">· {i18nT('app.subagent_cap', { cap: String(subagentCap) })}</span>
        )}
        <span id={descId} hidden>{summary}{capLine ? ` ${capLine}` : ''}</span>
      </button>
      <InstantTip tip={tip} tipId={tipId} className="w-max max-w-[min(22rem,calc(100vw-1rem))] whitespace-normal">
        <div className="text-text" data-testid="resource-health-tip">{tipBody}</div>
      </InstantTip>
    </>
  )
}
