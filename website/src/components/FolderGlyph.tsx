import { Folder, FolderOpen } from 'lucide-react'
import { folderColorStroke, folderColorWash } from './folderColorPaint'

// Default classes carry the stroke color: the icon paints via currentColor, so
// the root's text color IS the outline color — muted/70 at rest like the
// resting rail icons, stepping up to full muted when the row (a `group`) is
// hovered. Callers that pass className own the color instead.
const _FOLDER_GLYPH_CLASS = 'shrink-0 text-muted/70 group-hover:text-muted transition-colors'

/** The sidebar folder glyph: lucide's Folder/FolderOpen, tinted by the
 *  folder's palette color — stroke pulled toward text-strong for rail-icon
 *  contrast, body washed with the color over the theme surface. Only the
 *  CLOSED shape takes the wash: FolderOpen's flap overlaps its body, so any
 *  fill paints the overlap as a solid slab; the open state stays stroke-only,
 *  which also reads lighter while the folder's contents are on screen.
 *  Shared by the sidebar rows and the folder-settings modal's live preview. */
export default function FolderGlyph({ color, size = 20, open = false, className = _FOLDER_GLYPH_CLASS, testId }: { color?: string; size?: number; open?: boolean; className?: string; testId?: string }) {
  const Icon = open ? FolderOpen : Folder
  return (
    <span data-testid={testId} aria-hidden className={`relative inline-flex items-center justify-center ${className}`} style={{ width: size, height: size, ...(color ? { color: folderColorStroke(color) } : {}) }}>
      <Icon size={size} strokeWidth={2} fill={open ? 'none' : color ? folderColorWash(color) : 'var(--bg-elevated)'} style={{ transition: 'fill .2s' }} />
    </span>
  )
}
