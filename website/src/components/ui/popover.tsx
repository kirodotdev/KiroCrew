import * as React from 'react'
import * as PopoverPrimitive from '@radix-ui/react-popover'

import { cn } from '../../lib/utils'

const Popover = PopoverPrimitive.Root
const PopoverTrigger = PopoverPrimitive.Trigger
const PopoverAnchor = PopoverPrimitive.Anchor

/** `default` is the padded card; `list` is a picker list (a tight inset, the
 *  larger Glass radius and a deeper shadow), so a list caller does not restyle
 *  the primitive from outside. */
const POPOVER_VARIANTS = {
  default: 'rounded-md p-4 shadow-md',
  list: 'rounded-2xl p-1.5 shadow-lg',
} as const

const PopoverContent = React.forwardRef<
  React.ComponentRef<typeof PopoverPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content> & { variant?: keyof typeof POPOVER_VARIANTS }
>(({ className, align = 'center', sideOffset = 4, variant = 'default', ...props }, ref) => (
  <PopoverPrimitive.Portal>
    <PopoverPrimitive.Content
      ref={ref}
      align={align}
      sideOffset={sideOffset}
      className={cn(
        // Entry animation only. Radix suspends unmount until an exit animation
        // finishes, and the still-mounted dismissable layer consumes the next
        // pointer-down — so an exit animation makes a re-click on the trigger a
        // no-op for the animation's whole duration.
        'z-[9999] w-72 border border-border bg-bg-elevated text-text outline-hidden data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2',
        POPOVER_VARIANTS[variant],
        className
      )}
      {...props}
    />
  </PopoverPrimitive.Portal>
))
PopoverContent.displayName = PopoverPrimitive.Content.displayName

export { Popover, PopoverTrigger, PopoverContent, PopoverAnchor }
