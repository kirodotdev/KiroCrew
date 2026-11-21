import { cloneElement, isValidElement, useId, type FocusEvent, type ReactElement, type ReactNode } from 'react'

import { InstantTip, keyboardFocused, useInstantTip } from './InstantTip'

/**
 * Instant hover/focus tooltip for one control, replacing a native `title`
 * (whose ~1s OS delay reads as "no tooltip"). Timing, touch and Escape rules
 * are `useInstantTip`'s.
 *
 * The handlers sit on a wrapper `<span>` so the child can be anything — a
 * plain button, a Radix trigger, a component that owns its own props. React
 * focus events bubble, so the wrapper still sees a tab stop on the control.
 * Only a focus the browser would ring opens the hint (`keyboardFocused`), not
 * the focus a dialog or menu hands back to its trigger after a mouse close.
 * The child keeps its `aria-label` and gains `aria-describedby` pointing at a
 * `hidden` copy of the label that is always in the DOM — the bubble mounts only
 * while open, so pointing at it would leave the control without a description
 * the rest of the time, which the `title` never did. `hidden` rather than
 * `sr-only`: a description target is read even when hidden, and an `sr-only`
 * span would ALSO be read inline as page text (a heading would read as
 * "<title> Rename this artifact"). The child must not also carry a `title`, or
 * the native tooltip shows a second time.
 *
 * No `label` turns the hint off: the child renders untouched inside the
 * same wrapper, so a caller may switch the hint on and off without remounting
 * the control or changing the tree around it. The handlers stay on, so a
 * pointer that leaves while the hint is off still closes it.
 *
 * `className` goes on the wrapper, for layout that belongs to the parent's
 * direct child: a flex track (the top-bar search trigger's `flex-1`) or a
 * child selector (the top-bar capsule's `.tb-capsule-keep`).
 */
export default function HoverTip({ label, className, children }: {
  label?: ReactNode
  className?: string
  children: ReactElement<{ 'aria-describedby'?: string }>
}) {
  const { tip, tipHandlers, tipId } = useInstantTip({ placement: 'below' })
  const descId = useId()
  const { 'aria-describedby': _bubbleId, onFocus, ...rest } = tipHandlers
  void _bubbleId
  const handlers = { ...rest, onFocus: (e: FocusEvent) => { if (keyboardFocused(e.target)) onFocus(e) } }
  return (
    <span data-hover-tip="" className={className ? `inline-flex ${className}` : 'inline-flex'} {...handlers}>
      {label !== undefined && isValidElement(children) ? cloneElement(children, { 'aria-describedby': descId }) : children}
      {label !== undefined && (<>
        <span id={descId} hidden>{label}</span>
        <InstantTip tip={tip} tipId={tipId} className="w-max max-w-[min(26rem,calc(100vw-1rem))] whitespace-normal break-words">{label}</InstantTip>
      </>)}
    </span>
  )
}
