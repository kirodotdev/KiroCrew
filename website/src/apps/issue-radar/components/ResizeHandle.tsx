import type { usePointerDrag } from '../../../hooks/usePointerDrag'

/** The 6px vertical drag strip on a workspace column's right edge. Shared by
 * the left rail and the issue / PR list so every column edge looks and behaves
 * identically. */
export default function ResizeHandle({
  handleProps, label,
}: {
  handleProps: ReturnType<typeof usePointerDrag>
  label: string
}) {
  return (
    <div
      {...handleProps}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      title="Drag to resize"
      className="w-1.5 flex-shrink-0 cursor-col-resize hover:bg-accent/30 transition-colors"
      style={{ touchAction: 'none' }}
    />
  )
}
