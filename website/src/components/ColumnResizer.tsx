import { useEffect, useRef } from 'react'

import { usePointerDrag } from '../hooks/usePointerDrag'
import type { ColumnResizerBinding } from '../hooks/useTableColumnWidths'
import { i18nT } from '../i18n/t'

/** Px moved per arrow press, and per Shift+arrow press: the same steps as the
 *  layout-column grip (components/ResizeHandle), so both feel alike. */
const STEP = 16
const COARSE_STEP = 64

/**
 * The drag grip on the right edge of a resizable TABLE column's header cell.
 * Pair it with `useTableColumnWidths`, spreading `widths.resizer(key)`:
 *
 *   <TableHead className="w-[68px] relative" style={cols.style('id')}>
 *     {label}
 *     <ColumnResizer column={label} {...cols.resizer('id')} />
 *   </TableHead>
 *
 * The host cell must carry `relative`: the grip is absolutely positioned and
 * resolves against the nearest positioned ancestor, so without it the grip
 * lands on whatever box happens to be positioned further up. It is spelled at
 * the CALL SITE as a literal rather than concatenated in, because
 * `shadcn/require-static-classes` cannot check a className a component builds
 * from an opaque value, and a table's header classes are exactly what that rule
 * exists to keep readable. `SchedulePage.columnContract.test.ts` holds every
 * resizable header to it.
 *
 * A sibling of components/ResizeHandle rather than a variant of it: that one is
 * an in-flow flex child between two panes, this one is absolutely positioned
 * INSIDE the cell it resizes, because a table row has no slot between two
 * cells to put a splitter in. The visual language is shared on purpose (a 6px
 * hit strip carrying a 2px bar, `resize-accent` so a theme can retint it).
 *
 * Unlike the pane grip, every grip in the row shows a faint rule while the
 * pointer is anywhere over the table head: a pane's edge is a place users
 * already try to drag, but nothing about a plain header cell says its boundary
 * moves, and lighting only the hovered cell's rule reads as one decoration
 * rather than as a row of boundaries. It keys off the `thead` ancestor so a
 * caller has no row-level class to remember.
 *
 * It is the ARIA window-splitter widget: focusable, arrow-key operable, and it
 * reports its position. Enter and a double-click both return the column to its
 * declared default, so the keyboard can reach every state the mouse can.
 */
export default function ColumnResizer({
  column, value, min, max, onResize, onReset,
}: ColumnResizerBinding & {
  /** The column's already-translated header label, for the accessible name. */
  column: string
}) {
  const startRef = useRef(0)
  const draggingRef = useRef(false)
  // usePointerDrag reads its options through a ref, but the drag origin must be
  // the width at pointer-down, not at whichever render created the callback.
  const valueRef = useRef(value)
  valueRef.current = value

  const drag = usePointerDrag({
    threshold: 0,
    onStart: () => {
      startRef.current = valueRef.current
      draggingRef.current = true
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
    },
    onMove: ({ dx }) => onResize(startRef.current + dx, false),
    onEnd: ({ dx }) => {
      draggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      onResize(startRef.current + dx, true)
    },
  })

  // Unmount guard, as in useColumnResize: onEnd cannot fire once the element is
  // gone (a filter emptying the table mid-drag), which would strand the body's
  // resize cursor and selection lock.
  useEffect(() => () => {
    if (draggingRef.current) {
      draggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
  }, [])

  return (
    // A FOCUSABLE separator is the window-splitter widget, which owns a tab
    // stop and the keys below; jsx-a11y only models the static separator.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- focusable separator = the window-splitter widget; its key and pointer handlers ARE its documented operation
    <div
      {...drag}
      role="separator"
      aria-orientation="vertical"
      aria-label={i18nT('components.columnResizer.resize_column', { column })}
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- the splitter widget is operable from the keyboard, so it needs the tab stop
      tabIndex={0}
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      title={i18nT('components.columnResizer.resize_hint')}
      data-testid="column-resizer"
      // A header cell is often itself a click target's parent (row selection,
      // sort); neither gesture on the grip may reach it.
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => { e.stopPropagation(); onReset() }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') { e.preventDefault(); onReset(); return }
        // Left/Right only: swallowing Up/Down would break scrolling the page.
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
        e.preventDefault()
        const step = e.shiftKey ? COARSE_STEP : STEP
        onResize(value + (e.key === 'ArrowRight' ? step : -step), true)
      }}
      // No z-index, deliberately. A pinned `sticky` column has none either, and
      // relies on coming LAST in DOM order to paint over the cells scrolling
      // under it; a lifted grip would draw through that pinned header.
      className="group/drag absolute right-0 top-0 bottom-0 flex w-1.5 cursor-col-resize select-none items-center justify-center focus-ring rounded-full"
      style={{ touchAction: 'none' }}
    >
      <div
        aria-hidden="true"
        className="h-[60%] w-[2px] rounded-full bg-transparent transition-colors duration-200 resize-accent [thead:hover_&]:bg-border-strong group-hover/drag:bg-accent group-focus-visible/drag:bg-accent group-active/drag:bg-accent-hover"
      />
    </div>
  )
}
