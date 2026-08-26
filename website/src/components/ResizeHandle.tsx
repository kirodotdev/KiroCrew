import type { usePointerDrag } from '../hooks/usePointerDrag'

import { i18nT } from '../i18n/t'

/** Px moved per arrow press, and per Shift+arrow press. */
const STEP = 16
const COARSE_STEP = 64

/** The 6px vertical drag strip on a resizable column's right edge. Shared across
 * the workspace-style surfaces (Issue Radar's rail and issue / PR lists, Task
 * Runner's run rail) so every column edge looks and behaves identically. Pair it
 * with `useColumnResize`, which supplies `handleProps` and `onNudge`.
 *
 * It is the ARIA window-splitter pattern, not a decorative divider: focusable,
 * arrow-key operable, and reporting its position. Pointer-only would leave
 * keyboard users unable to resize at all — and on a collapsible column, unable
 * to reach the layout the mouse can. `value`/`min`/`max` are optional so the
 * caller can omit them for a splitter whose extent isn't meaningful, but pass
 * them when you have them: without `aria-valuenow` a screen reader announces a
 * splitter with no position. */
export default function ResizeHandle({
  handleProps, label, onNudge, value, min, max,
}: {
  handleProps: ReturnType<typeof usePointerDrag>
  label: string
  /** Called with a px delta on arrow keys. Omit for a pointer-only handle. */
  onNudge?: (dx: number) => void
  value?: number
  min?: number
  max?: number
}) {
  return (
    <div
      {...handleProps}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
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
      className="w-1.5 flex-shrink-0 cursor-col-resize hover:bg-accent/30 focus-visible:bg-accent/50 focus-ring transition-colors"
      style={{ touchAction: 'none' }}
    />
  )
}
