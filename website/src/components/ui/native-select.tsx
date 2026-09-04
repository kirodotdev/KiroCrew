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
interface NativeSelectProps extends React.ComponentPropsWithoutRef<'select'> {
  /** Inline style for the positioning wrapper — where a caller's sizing belongs. */
  wrapperStyle?: React.CSSProperties
}

/**
 * Right padding the control reserves for the chevron overlay: the arrow is 14px
 * wide and pinned at `right-3` (12px), so 36px clears it with the same breathing
 * room the old `pr-9` class gave. Not exported — a test that imported it could
 * only assert the padding equals the constant it was set from, which is true by
 * construction; the test states `2.25rem` itself so a changed value reddens it.
 */
const CHEVRON_GUTTER = '2.25rem'

const NativeSelect = React.forwardRef<HTMLSelectElement, NativeSelectProps>(
  // Named function expression rather than an arrow plus `NativeSelect.displayName = '…'`:
  // React infers the devtools name from the function, and the assignment form would
  // add a bare string literal that the i18n gate counts (eslint-rules/i18n-strict.js
  // re-surfaces literals the upstream rule exempts).
  function NativeSelect({ className, wrapperStyle, children, ...props }, ref) {
    return (
      // The wrapper is the layout box: it owns the chevron's positioning context,
      // so a caller's sizing (a flex basis, a min-width) has to land HERE to have
      // any effect — on the `<select>` itself, which is `w-full` inside this div,
      // a flex rule is inert.
      <div className="relative" style={wrapperStyle}>
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
            'flex items-center justify-between w-full pl-3 py-2 rounded-md text-base border border-border bg-bg-elevated text-text truncate',
            'hover:border-border-strong transition-all cursor-pointer outline-none appearance-none',
            'focus-visible:border-accent disabled:opacity-40 disabled:pointer-events-none',
            className
          )}
          // The gutter the chevron below sits in. It is a constant of THIS
          // component (the arrow is always `right-3` and always 14px), not a
          // caller's choice — and it is inline rather than a `pr-9` class because
          // `cn` is tailwind-merge, which cannot split a caller's `px-*`
          // shorthand: the two classes would both survive and which one won would
          // depend on Tailwind's stylesheet order. A caller passing `px-1.5` is
          // how the arrow came to paint over the text on the phone sidebar's
          // recency-unit picker. `style` is the wrapper's on this component, so
          // nothing of the caller's is being overwritten here.
          style={{ paddingInlineEnd: CHEVRON_GUTTER }}
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
