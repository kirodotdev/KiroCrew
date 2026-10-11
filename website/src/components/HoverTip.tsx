import { cloneElement, isValidElement, useId, type ReactElement, type ReactNode } from 'react'

import { InstantTip, useInstantTip } from './InstantTip'

/**
 * Instant hover/focus tooltip for one control, replacing a native `title`
 * (whose ~1s OS delay reads as "no tooltip"). Timing, touch and Escape rules
 * are `useInstantTip`'s.
 *
 * The handlers sit on a wrapper `<span>` so the child can be anything — a
 * plain button, a Radix trigger, a component that owns its own props. React
 * focus events bubble, so the wrapper still sees a tab stop on the control.
 * The child keeps its `aria-label` and gains `aria-describedby` pointing at a
 * `hidden` copy of the label that is always in the DOM — the bubble mounts only
 * while open, so pointing at it would leave the control without a description
 * the rest of the time, which the `title` never did. `hidden` rather than
 * `sr-only`: a description target is read even when hidden, and an `sr-only`
 * span would ALSO be read inline as page text (a heading would read as
 * "<title> Rename this artifact"). The child must not also carry a `title`, or
 * the native tooltip shows a second time.
 */
export default function HoverTip({ label, children }: {
  label: ReactNode
  children: ReactElement<{ 'aria-describedby'?: string }>
}) {
  const { tip, tipHandlers, tipId } = useInstantTip({ placement: 'below' })
  const descId = useId()
  const { 'aria-describedby': _bubbleId, ...handlers } = tipHandlers
  void _bubbleId
  return (
    <span data-hover-tip="" className="inline-flex" {...handlers}>
      {isValidElement(children) ? cloneElement(children, { 'aria-describedby': descId }) : children}
      <span id={descId} hidden>{label}</span>
      <InstantTip tip={tip} tipId={tipId} className="whitespace-nowrap">{label}</InstantTip>
    </span>
  )
}
