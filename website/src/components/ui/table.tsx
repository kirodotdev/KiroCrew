import * as React from 'react'
import { cn } from '../../lib/utils'

/**
 * shadcn/ui `table`, kept at UPSTREAM typography and spacing.
 *
 * Only the COLOURS are re-pointed at this repo's tokens — shadcn's stock palette
 * names (`--foreground`, `--muted`, `--muted-foreground`) do not exist here, so
 * every one of them would render as an unstyled fallback. The mapping is
 * mechanical:
 *
 *   text-foreground        -> text-text-strong
 *   text-muted-foreground  -> text-muted
 *   hover:bg-muted/50      -> hover:bg-bg-hover
 *   bg-muted (selected)    -> bg-bg-hover
 *   bg-muted/50 (footer)   -> bg-bg-elevated
 *   border                 -> border-border
 *
 * Everything else is upstream verbatim, including the two bits this repo used to
 * drop and now keeps:
 *
 *   - `whitespace-nowrap` on `TableHead` / `TableCell`. Upstream never wraps a
 *     cell; a wide table scrolls horizontally in the wrapper instead. That is a
 *     real behavioural choice, not decoration — it trades "no cell ever folds
 *     into four lines" for "the table can extend past the viewport".
 *   - `[&:has([role=checkbox])]:pr-0` and `[&>[role=checkbox]]:translate-y-[2px]`,
 *     upstream's padding + 2px optical fix for a checkbox column.
 *
 * `data-[state=selected]` is also kept: a caller that marks a row selected gets
 * the variant instead of hand-rolling its own background class.
 *
 * Pure markup plus `cn()`: unlike the other components in this directory there
 * is no Radix primitive behind a table, so adding it costs no new dependency.
 *
 * One unavoidable deviation from upstream: each `forwardRef` takes a NAMED
 * function expression so React derives the devtools name from it. Upstream
 * assigns `X.displayName = 'X'`, and a bare `'Table'` literal trips the i18n
 * added-lines gate, which cannot tell a debug name from user copy.
 */

const Table = React.forwardRef<
  HTMLTableElement,
  React.HTMLAttributes<HTMLTableElement>
>(function Table({ className, ...props }, ref) {
  return (
    <div className="relative w-full overflow-x-auto">
      <table
        ref={ref}
        className={cn('w-full caption-bottom text-sm', className)}
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
      className={cn('border-t border-border bg-bg-elevated font-medium [&>tr]:last:border-b-0', className)}
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
      className={cn(
        'border-b border-border transition-colors hover:bg-bg-hover data-[state=selected]:bg-bg-hover',
        className,
      )}
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
        'h-10 whitespace-nowrap px-2 text-left align-middle font-medium text-text-strong',
        '[&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]',
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
  return (
    <td
      ref={ref}
      className={cn(
        'whitespace-nowrap p-2 align-middle',
        '[&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]',
        className,
      )}
      {...props}
    />
  )
})

const TableCaption = React.forwardRef<
  HTMLTableCaptionElement,
  React.HTMLAttributes<HTMLTableCaptionElement>
>(function TableCaption({ className, ...props }, ref) {
  return <caption ref={ref} className={cn('mt-4 text-sm text-muted', className)} {...props} />
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
