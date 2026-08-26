import * as React from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '../../lib/utils'

/**
 * A styled native `<select>` — shadcn's `native-select` primitive, in this
 * codebase's idiom and sitting beside `ui/select.tsx` (the Radix popup) the way
 * shadcn ships the two side by side. Their guidance for choosing:
 *
 *   NativeSelect — native browser behaviour, better performance, mobile-optimised
 *   Select       — custom styling, animations, complex interactions
 *
 * The platform draws the option list, so it scrolls, type-aheads and is
 * accessible without any of our code being involved. That is the point: the
 * Radix popup's list is a `position:fixed` overflow scroller inside a scroll
 * lock, which a finger drag does not reliably move on iOS Safari.
 *
 * `color-scheme` is already declared per theme on the root in index.css, so the
 * OS-drawn popup follows the app's light/dark setting for free.
 *
 * NOT ported from shadcn: `NativeSelectOptGroup`. Nothing here has grouped
 * options, and an unused export is API to maintain for no caller.
 */
const NativeSelect = React.forwardRef<HTMLSelectElement, React.ComponentPropsWithoutRef<'select'>>(
  // Named function expression rather than an arrow plus `NativeSelect.displayName = '…'`:
  // React infers the devtools name from the function, and the assignment form would
  // add a bare string literal that the i18n gate counts (eslint-rules/i18n-strict.js
  // re-surfaces literals the upstream rule exempts).
  function NativeSelect({ className, children, ...props }, ref) {
    return (
      <div className="relative">
        <select
          ref={ref}
          // Mirrors ui/select.tsx's SelectTrigger so the two paths are the same
          // control to look at. `appearance-none` plus the chevron below replaces
          // the platform's own arrow, which would otherwise sit beside ours.
          //
          // `text-base` (16px) rather than the trigger's `text-sm`: iOS Safari
          // zooms the page when a form control under 16px takes focus, the same
          // reason the composer bumps to 16px on coarse pointers.
          className={cn(
            'flex items-center justify-between w-full pl-3 pr-9 py-2 rounded-md text-base border border-border bg-bg-elevated text-text truncate',
            'hover:border-border-strong transition-all cursor-pointer outline-none appearance-none',
            'focus-visible:border-accent disabled:opacity-40 disabled:pointer-events-none',
            className
          )}
          {...props}
        >
          {children}
        </select>
        <ChevronDown className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-muted" size={14} aria-hidden />
      </div>
    )
  }
)

const NativeSelectOption = React.forwardRef<HTMLOptionElement, React.ComponentPropsWithoutRef<'option'>>(
  function NativeSelectOption({ className, ...props }, ref) {
    return <option ref={ref} className={cn('text-text', className)} {...props} />
  }
)

export { NativeSelect, NativeSelectOption }
