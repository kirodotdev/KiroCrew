// ColumnSplitter — the drag strip on a workspace column's edge.
//
// The shared `ResizeHandle` draws it, so this edge is the same 2px accent grip
// as every other column edge in the dashboard. Pointer drag comes from the
// shared useColumnResize/usePointerDrag pair (touch and pen included, via
// pointer capture); the arrow keys come from ResizeHandle's window-splitter
// semantics. This wrapper only adapts the caller's step unit: ResizeHandle
// nudges in px (16, or 64 with Shift), and `onNudge` here takes steps of 16px.
import type { usePointerDrag } from '../../../hooks/usePointerDrag'
import ResizeHandle from '../../../components/ResizeHandle'

import { i18nT } from '../../../i18n/t'
export interface ColumnSplitterProps {
  handleProps: ReturnType<typeof usePointerDrag>
  label: string
  /** Current position, for the value semantics. */
  valueNow: number
  valueMin: number
  valueMax: number
  /** Called with a signed step (one per 16px) when an arrow key is pressed. */
  onNudge: (delta: number) => void
}

export default function ColumnSplitter({
  handleProps, label, valueNow, valueMin, valueMax, onNudge,
}: ColumnSplitterProps) {
  return (
    <ResizeHandle
      handleProps={handleProps}
      label={label}
      title={i18nT('apps.specBuilder.components.columnSplitter.drag_or_use_to_resize')}
      onNudge={(dx) => onNudge(dx / 16)}
      value={Math.round(valueNow)}
      min={valueMin}
      max={valueMax}
    />
  )
}
