import type { usePointerDrag } from '../hooks/usePointerDrag'

import { i18nT } from '../i18n/t'
import { cn } from '../lib/utils'

/** Px moved per arrow press, and per Shift+arrow press. */
const STEP = 16
const COARSE_STEP = 64

/** The vertical drag grip on a resizable column's edge — one component so the
 * Sessions sidebar, the Crew Members roster and the workspace-style surfaces
 * (Issue Radar's rail and issue / PR lists, Task Runner's run rail, Projects)
 * all look and behave identically. The claim covers the surfaces that MOUNT
 * this component: Spec Builder's and Code Review Sage's column splitters still
 * draw their own flat strip and are tracked in #9580 to move onto it. Pair it
 * with `useColumnResize` (or any `usePointerDrag`) for `handleProps`, and
 * `onNudge` for the arrow keys.
 *
 * Visual contract: a 6px transparent hit strip carrying a 2px `rounded-full`
 * bar that stays invisible at rest, turns accent on hover and keyboard focus,
 * and accent-hover while dragging. `inset` trims the bar's ends — pass the
 * card's corner radius (12px for `rounded-xl`) when the grip runs along a
 * rounded card edge so the accent spans exactly the straight segment of the
 * border instead of overshooting the rounded corners.
 *
 * It is the ARIA window-splitter pattern, not a decorative divider: focusable,
 * arrow-key operable, and reporting its position. Pointer-only would leave
 * keyboard users unable to resize at all — and on a collapsible column, unable
 * to reach the layout the mouse can. `value`/`min`/`max` are optional so the
 * caller can omit them for a splitter whose extent isn't meaningful, but pass
 * them when you have them: without `aria-valuenow` a screen reader announces a
 * splitter with no position. */
export default function ResizeHandle({
  handleProps, label, onNudge, value, min, max, inset = 0, className,
}: {
  handleProps: ReturnType<typeof usePointerDrag>
  label: string
  /** Called with a px delta on arrow keys. Omit for a pointer-only handle. */
  onNudge?: (dx: number) => void
  value?: number
  min?: number
  max?: number
  /** Px trimmed from each end of the visible bar (the host card's corner radius). */
  inset?: number
  /** Positioning overrides for the hit strip — e.g. `absolute` placement on a
   *  card's border instead of the default in-flow flex sibling. Merged with
   *  tailwind-merge, so a `w-*` here replaces the default hit width. */
  className?: string
}) {
  return (
    // ARIA gives `separator` two flavours and jsx-a11y only models the static
    // one: a FOCUSABLE separator is the window-splitter widget, which owns both
    // a tab stop and the arrow keys below. The rule cannot tell the two apart,
    // so it reads the widget as decorative furniture.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- focusable separator = the window-splitter widget; onKeyDown IS its documented operation
    <div
      {...handleProps}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- the tab stop exists only when `onNudge` makes the splitter operable, which is what promotes it to a widget
      tabIndex={onNudge ? 0 : undefined}
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      onKeyDown={onNudge
        ? (e) => {
          // Left/Right only: a vertical splitter moves horizontally, and
          // swallowing Up/Down would break scrolling the columns either side.
          if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
          e.preventDefault()
          const step = e.shiftKey ? COARSE_STEP : STEP
          onNudge(e.key === 'ArrowRight' ? step : -step)
        }
        : undefined}
      title={i18nT('components.resizeHandle.drag_to_resize')}
      data-testid="resize-handle"
      className={cn(
        // 6px hit strip: wider than the 2px bar it shows so the grip is easy to
        // catch — the bar, not the strip, is what the user sees.
        'group/drag relative flex w-1.5 shrink-0 items-center justify-center cursor-col-resize select-none focus-ring rounded-full',
        className,
      )}
      style={{ touchAction: 'none' }}
    >
      {/* Visual bar only — the strip above stays the full-height hit area.
          `resize-accent` lets a theme retint the hover colour (the Kiro dark
          theme does) without reaching into this component. */}
      <div
        aria-hidden="true"
        data-testid="resize-handle-bar"
        className="w-[2px] rounded-full bg-transparent transition-colors duration-200 resize-accent group-hover/drag:bg-accent group-focus-visible/drag:bg-accent group-active/drag:bg-accent-hover"
        style={{ height: inset > 0 ? `calc(100% - ${inset * 2}px)` : '100%' }}
      />
    </div>
  )
}
