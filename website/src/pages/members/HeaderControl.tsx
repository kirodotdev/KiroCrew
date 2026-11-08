import { forwardRef, type ButtonHTMLAttributes } from 'react'
import Glass from '../../components/Glass'

type HeaderControlProps = {
  /** In the phone shell's top bar rather than over the pane. */
  inBar: boolean
  chipClassName: string
  barClassName: string
} & ButtonHTMLAttributes<HTMLButtonElement> & { [data: `data-${string}`]: string | undefined }

/**
 * A thread-header control: a Glass chip over the pane, where it sits on
 * scrolled text, and a bare icon button in the phone shell's top bar, like the
 * Sessions bar's controls and the bell beside it. Forwards its ref and every
 * other prop to the button, so a `uiLocation` marker lands on what is tapped.
 */
const HeaderControl = forwardRef<HTMLButtonElement, HeaderControlProps>(function HeaderControl(
  { inBar, chipClassName, barClassName, ...rest }, ref,
) {
  return inBar
    ? <button ref={ref} type="button" className={barClassName} {...rest} />
    : <Glass ref={ref} as="button" variant="chip" radius={999} className={chipClassName} {...rest} />
})

export default HeaderControl
