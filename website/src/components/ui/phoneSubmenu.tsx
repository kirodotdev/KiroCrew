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
}

/** Inline trigger row: a `role="button"` div composing caller handlers with the toggle. */
export const PhoneSubTriggerDiv = React.forwardRef<HTMLDivElement, PhoneSubTriggerDivProps>(
  ({ className, inset, expanded, onToggle, onClick, onKeyDown, children, ...rest }, ref) => (
    <div
      {...rest}
      ref={ref}
      role="button"
      tabIndex={0}
      aria-expanded={expanded}
      onClick={(e) => {
        ;(onClick as unknown as React.MouseEventHandler<HTMLDivElement> | undefined)?.(e)
        e.preventDefault()
        onToggle()
      }}
      onKeyDown={(e) => {
        ;(onKeyDown as unknown as React.KeyboardEventHandler<HTMLDivElement> | undefined)?.(e)
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
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
  ),
)
PhoneSubTriggerDiv.displayName = 'PhoneSubTriggerDiv'

/** Inline submenu body rendered beneath its trigger when expanded. */
export const PhoneSubContentDiv = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, children, ...rest }, ref) => (
  <div
    {...rest}
    ref={ref}
    className={cn(
      'mt-1 ml-3 border-l border-border pl-2 space-y-0.5 overflow-hidden rounded-md bg-bg-elevated p-1',
      className,
    )}
  >
    {children}
  </div>
))
PhoneSubContentDiv.displayName = 'PhoneSubContentDiv'
