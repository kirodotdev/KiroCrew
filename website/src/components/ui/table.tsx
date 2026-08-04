import * as React from 'react'
import { cn } from '../../lib/utils'

/**
 * shadcn/ui `table`, themed to this repo's tokens rather than shadcn's stock
 * zinc palette — the same treatment ui/select.tsx and ui/dropdown-menu.tsx got.
 *
 * Pure markup plus `cn()`: unlike the other components in this directory there
 * is no Radix primitive behind a table, so adding it costs no new dependency.
 *
 * Three deliberate deviations from upstream shadcn:
 *
 *   - Each `forwardRef` takes a NAMED function expression, so React derives the
 *     devtools name from it. Upstream assigns `X.displayName = 'X'`, and every
 *     sibling in this directory instead borrows the name from the Radix
 *     primitive it wraps (`SelectPrimitive.Trigger.displayName`). A table has no
 *     primitive to borrow from, and a bare `'Table'` literal trips the i18n
 *     added-lines gate, which cannot tell a debug name from user copy. Naming
 *     the function gets the same devtools output with no literal and no
 *     lint exemption.
 *   - `TableRow` drops `data-[state=selected]`. Nothing in this repo drives a
 *     selection state on a row, and a dangling variant reads as a feature that
 *     exists.
 *   - The wrapper is `overflow-x-auto`, not `overflow-auto`. `overflow-auto`
 *     establishes a scroll container on BOTH axes, which clips any popover or
 *     tooltip a cell renders instead of letting it escape the table.
 */

const Table = React.forwardRef<
  HTMLTableElement,
  React.HTMLAttributes<HTMLTableElement>
>(function Table({ className, ...props }, ref) {
  return (
    <div className="relative w-full overflow-x-auto">
      <table
        ref={ref}
        className={cn('w-full caption-bottom border-collapse text-[13px]', className)}
        {...props}
      />
    </div>
  )
})

const TableHeader = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(function TableHeader({ className, ...props }, ref) {
  return <thead ref={ref} className={cn('[&_tr]:border-b [&_tr]:border-border', className)} {...props} />
})

const TableBody = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(function TableBody({ className, ...props }, ref) {
  return <tbody ref={ref} className={cn('[&_tr:last-child]:border-0', className)} {...props} />
})

const TableFooter = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(function TableFooter({ className, ...props }, ref) {
  return (
    <tfoot
      ref={ref}
      className={cn('border-t border-border bg-bg-accent font-medium [&>tr]:last:border-b-0', className)}
      {...props}
    />
  )
})

const TableRow = React.forwardRef<
  HTMLTableRowElement,
  React.HTMLAttributes<HTMLTableRowElement>
>(function TableRow({ className, ...props }, ref) {
  return (
    <tr
      ref={ref}
      className={cn('border-b border-border transition-colors hover:bg-bg-hover', className)}
      {...props}
    />
  )
})

const TableHead = React.forwardRef<
  HTMLTableCellElement,
  React.ThHTMLAttributes<HTMLTableCellElement>
>(function TableHead({ className, ...props }, ref) {
  return (
    <th
      ref={ref}
      className={cn(
        'h-9 px-3 text-left align-middle text-[10px] font-semibold uppercase tracking-wider text-muted',
        className,
      )}
      {...props}
    />
  )
})

const TableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement>
>(function TableCell({ className, ...props }, ref) {
  return <td ref={ref} className={cn('px-3 py-2.5 align-middle', className)} {...props} />
})

const TableCaption = React.forwardRef<
  HTMLTableCaptionElement,
  React.HTMLAttributes<HTMLTableCaptionElement>
>(function TableCaption({ className, ...props }, ref) {
  return <caption ref={ref} className={cn('mt-3 text-[12px] text-muted', className)} {...props} />
})

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
}
