import * as React from 'react'
import { cn } from '../../lib/utils'

/**
 * Shared coarse-pointer (phone) submenu primitives.
 *
 * `@radix-ui/react-menu` hardcodes submenus to `side="right"`, so beside a
 * phone-wide menu the flyout lands off-screen. On coarse pointers both menu
 * families render the submenu inline instead: `Sub` becomes a plain `div`,
 * the trigger toggles an expanded flag, and the content renders beneath.
 * The logic is identical for dropdown and context menus — only the Radix
 * primitives differ — so it lives here once instead of cloned per family.
 */

/** Controlled / uncontrolled expanded state honouring the Radix Sub contract. */
export function usePhoneSubState(
  open: boolean | undefined,
  defaultOpen: boolean | undefined,
  onOpenChange: ((open: boolean) => void) | undefined,
): { expanded: boolean; toggle: () => void } {
  const [uncontrolledExpanded, setUncontrolledExpanded] = React.useState(!!defaultOpen)
  const isControlled = open !== undefined
  const expanded = isControlled ? !!open : uncontrolledExpanded
  const toggle = React.useCallback(() => {
    const next = !expanded
    if (!isControlled) setUncontrolledExpanded(next)
    onOpenChange?.(next)
  }, [expanded, isControlled, onOpenChange])
  return { expanded, toggle }
}

export interface PhoneSubTriggerDivProps extends React.HTMLAttributes<HTMLDivElement> {
  inset?: boolean
  expanded: boolean
  onToggle: () => void
  /**
   * Honoured here rather than passed through. Radix's own SubTrigger implements
   * `disabled`, but this touch branch replaces it with a plain div, where the
   * attribute is inert — so a caller's `disabled` silently opened the submenu on
   * every touch device. Kept off `pointer-events` on purpose: removing them would
   * also remove hover, and hover is what renders the offline `title`.
   */
  disabled?: boolean
}

/** Inline trigger row: a `role="button"` div composing caller handlers with the toggle. */
export const PhoneSubTriggerDiv = React.forwardRef<HTMLDivElement, PhoneSubTriggerDivProps>(
  function PhoneSubTriggerDiv(
    { className, inset, expanded, onToggle, onClick, onKeyDown, children, disabled, 'aria-disabled': ariaDisabled, ...rest },
    ref,
  ) {
    // Either signal gates it. Focus is NOT removed: a gated row the user can
    // reach is how the offline reason gets announced rather than skipped.
    const gated = disabled === true || ariaDisabled === true || ariaDisabled === 'true'
    return (
      <div
      {...rest}
      ref={ref}
      role="button"
      tabIndex={0}
      aria-disabled={gated || undefined}
      data-disabled={gated ? '' : undefined}
      aria-expanded={expanded}
      onClick={(e) => {
        ;(onClick as unknown as React.MouseEventHandler<HTMLDivElement> | undefined)?.(e)
        e.preventDefault()
        if (gated) return
        onToggle()
      }}
      onKeyDown={(e) => {
        ;(onKeyDown as unknown as React.KeyboardEventHandler<HTMLDivElement> | undefined)?.(e)
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          if (gated) return
          onToggle()
        }
      }}
      className={cn(
        'relative flex cursor-pointer select-none items-center gap-2 rounded-md px-3 py-1.5 text-[13px] outline-none transition-colors',
        'focus:bg-bg-hover data-[state=open]:bg-bg-hover',
        expanded && 'bg-bg-hover',
        inset && 'pl-8',
        className,
      )}
    >
      {children}
      </div>
    )
  },
)


/** Inline submenu body rendered beneath its trigger when expanded. */
export const PhoneSubContentDiv = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(function PhoneSubContentDiv({ className, children, ...rest }, ref) {
  return (
    <div
      {...rest}
      ref={ref}
      className={cn(
        'mt-1 ml-3 border-l border-border pl-2 space-y-0.5 overflow-y-auto overscroll-contain rounded-md bg-bg-elevated p-1',
        className,
      )}
    >
      {children}
    </div>
  )
})
