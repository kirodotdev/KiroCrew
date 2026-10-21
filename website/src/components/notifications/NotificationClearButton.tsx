import { forwardRef, useId, useState, type ReactNode } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { Trash2, X } from 'lucide-react'

import { i18nT } from '../../i18n/t'

/** One height for every control on a notification header row: this button
 *  and the stack header's Show less (`MAC_ACTION_BTN_CLASS` resized to it),
 *  so a ⓧ never reads a size smaller than its neighbour. */
export const NOTIF_CONTROL_HEIGHT = 'h-[26px]'
const NOTIF_CONTROL_WIDTH = 'w-[26px]'
/** The same glass capsule surface as `MAC_ACTION_BTN_CLASS`. The bracketed
 *  `bg-[…]` token leads for the i18n lint (see that constant). */
const CLEAR_SURFACE = 'bg-[color-mix(in_srgb,var(--bg-hover)_80%,transparent)] backdrop-blur border border-[color-mix(in_srgb,var(--border)_45%,transparent)] transition-colors hover:bg-bg-hover'

export interface NotificationClearButtonProps {
  /** Visible idle text, passed as children so the UI location index reads it
   *  as the label. Omitted, the idle control is an icon-only glyph and
   *  `aria-label` names it. */
  children?: ReactNode
  /** The idle glyph: ⓧ, a trash can where an ⓧ would read as "close" (the
   *  bell sheet's top-right corner), or none for a text-only label. */
  icon?: 'x' | 'trash' | 'none'
  'aria-label'?: string
  /** Text the armed capsule shows ("Clear", "Clear all"). */
  confirmText: string
  /** Runs on the second press, never the first. */
  onClick: () => void
  /** Keeps the idle ⓧ visible at rest instead of on the host's hover. */
  alwaysVisible?: boolean
  className?: string
}

/**
 * macOS Notification Center's clear control. The first press turns the idle
 * glyph (or label) into a confirm capsule ("Clear", "Clear all"); the second
 * press clears. Leaving the control, moving focus away or pressing Escape turns
 * it back, so a stray press deletes nothing and no modal confirm is needed.
 *
 * It forwards its ref to the `<button>` that carries `data-ui-location`, so a
 * `{...uiLocation(id)}` spread registers that element as the UI location.
 *
 * The capsule IS the idle control: one element whose size animates between
 * the two states, so the eye follows it instead of seeing one control vanish
 * and another appear. Reduced motion drops the animation, not the continuity.
 *
 * Icon-only idle controls hide until the host (`group/clear`) is hovered or the
 * control is focused, and stay visible on touch screens and while armed.
 */
const NotificationClearButton = forwardRef<HTMLButtonElement, NotificationClearButtonProps>(function NotificationClearButton({
  children, icon = 'x', 'aria-label': ariaLabel, confirmText, onClick, alwaysVisible = false, className = '', ...rest
}, ref) {
  const [armed, setArmed] = useState(false)
  const reduceMotion = useReducedMotion()
  const hintId = useId()
  const iconOnly = !children
  const glyph = icon === 'trash' ? <Trash2 className="lucide-inline" /> : icon === 'x' ? <X className="lucide-inline" /> : null
  const hideAtRest = !armed && !alwaysVisible && iconOnly
  return (
    <>
    <button
      ref={ref}
      type="button"
      // Armed, the name is the visible confirm text ("Clear this group"), so
      // the words a person sees are the words assistive tech reads (label in
      // name); the idle aria-label still names an icon-only glyph.
      aria-label={armed ? undefined : ariaLabel}
      aria-describedby={armed ? hintId : undefined}
      data-armed={armed || undefined}
      // An icon-only idle control hides until its host (`group/clear`) is
      // hovered or it takes focus; a touch screen has no hover, so it shows.
      className={`shrink-0 bg-transparent border-none p-0 cursor-pointer font-body transition-opacity ${hideAtRest ? 'opacity-0 group-hover/clear:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100' : ''} ${className}`}
      onClick={() => {
        if (armed) { setArmed(false); onClick() } else setArmed(true)
      }}
      onMouseLeave={() => setArmed(false)}
      onBlur={() => setArmed(false)}
      onKeyDown={e => { if (e.key === 'Escape' && armed) { e.stopPropagation(); setArmed(false) } }}
      {...rest}
    >
      <motion.span
        layout
        transition={reduceMotion ? { duration: 0 } : { type: 'spring', stiffness: 520, damping: 38 }}
        // Set inline so `layout` animates it: the ⓧ is a circle, the armed
        // capsule takes Show less's 8 px (`rounded-lg`) corner.
        style={{ borderRadius: iconOnly && !armed ? 999 : 8 }}
        className={`${CLEAR_SURFACE} inline-flex items-center justify-center gap-1 whitespace-nowrap text-[12px] font-medium leading-none ${NOTIF_CONTROL_HEIGHT} ${armed
          ? 'px-3 text-text hover:text-danger-text'
          : iconOnly
            ? `${NOTIF_CONTROL_WIDTH} text-muted hover:text-text`
            : 'px-2.5 text-muted hover:text-danger-text'}`}
      >
        {armed ? confirmText : iconOnly ? glyph : glyph ? <>{glyph} {children}</> : children}
      </motion.span>
    </button>
    {/* Outside the button: inside, it would join the accessible name. */}
    {armed && <span id={hintId} className="sr-only">{i18nT('components.notifications.notificationFeed.press_again_to_clear')}</span>}
    </>
  )
})

export default NotificationClearButton
