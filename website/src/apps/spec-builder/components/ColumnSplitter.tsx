// ColumnSplitter — the drag strip on a workspace column's edge.
//
// Pointer drag comes from the shared useColumnResize/usePointerDrag pair (touch
// and pen included, via pointer capture). Keyboard resize is wired here because
// the W3C APG "window splitter" pattern is exactly role="separator" + tabIndex +
// aria-valuenow/min/max, and a separator that only responds to a pointer is
// unusable without one.
import type { usePointerDrag } from '../../../hooks/usePointerDrag'

import { i18nT } from '../../../i18n/t'
export interface ColumnSplitterProps {
  handleProps: ReturnType<typeof usePointerDrag>
  label: string
  /** Current position as a percentage, for the value semantics. */
  valueNow: number
  valueMin: number
  valueMax: number
  /** Called with a signed step when an arrow key is pressed. */
  onNudge: (delta: number) => void
}

export default function ColumnSplitter({
  handleProps, label, valueNow, valueMin, valueMax, onNudge,
}: ColumnSplitterProps) {
  return (
    /* eslint's non-interactive heuristics don't model the W3C APG "window
       splitter" pattern, which is precisely role="separator" + tabIndex +
       aria-valuenow/min/max and IS interactive once focusable. Suppressed
       rather than reshaped: a <button> here would announce the wrong role and
       lose the value semantics. */
    /* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */
    <div
      {...handleProps}
      onKeyDown={(e) => {
        if (e.key === 'ArrowLeft') { e.preventDefault(); onNudge(-1) }
        else if (e.key === 'ArrowRight') { e.preventDefault(); onNudge(1) }
      }}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={Math.round(valueNow)}
      aria-valuemin={valueMin}
      aria-valuemax={valueMax}
      tabIndex={0}
      title={i18nT('apps.specBuilder.components.columnSplitter.drag_or_use_to_resize')}
      className="w-1.5 shrink-0 cursor-col-resize hover:bg-accent/30 transition-colors focus-ring"
      style={{ touchAction: 'none' }}
    />
    /* eslint-enable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */
  )
}
