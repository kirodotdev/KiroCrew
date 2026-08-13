import * as React from 'react'
import * as ProgressPrimitive from '@radix-ui/react-progress'

import { cn } from '@/lib/utils'

/**
 * shadcn/ui Progress over @radix-ui/react-progress.
 *
 * Pass `value={null}` (or omit it) for the INDETERMINATE state -- Radix sets
 * `data-state="indeterminate"` and drops aria-valuenow, and the indicator
 * renders a sweeping segment instead of a fill. A filled-from-left bar with no
 * real value reads as actual progress and then visibly jumps when the first
 * measurement arrives.
 */
const Progress = React.forwardRef<
  React.ElementRef<typeof ProgressPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof ProgressPrimitive.Root>
>(({ className, value, ...props }, ref) => (
  <ProgressPrimitive.Root
    ref={ref}
    className={cn('relative h-1 w-full overflow-hidden rounded-full bg-border', className)}
    value={value}
    {...props}
  >
    <ProgressPrimitive.Indicator
      // Plain conditional, NOT a data-[state=...] Tailwind variant:
      // progress-indeterminate is a hand-written CSS class (index.css), and
      // Tailwind variants only generate registered utilities -- the variant
      // form compiles to nothing, silently.
      className={cn(
        'h-full bg-accent transition-transform duration-200',
        value == null && 'w-1/4 progress-indeterminate',
      )}
      style={value == null ? undefined : { transform: `translateX(-${100 - value}%)` }}
    />
  </ProgressPrimitive.Root>
))
Progress.displayName = ProgressPrimitive.Root.displayName

export { Progress }
