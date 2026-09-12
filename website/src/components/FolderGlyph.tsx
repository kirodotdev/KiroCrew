import { ChevronRight, Folder, FolderOpen } from 'lucide-react'
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
 *  When the folder carries an emoji `icon` (auto-generated or user-picked),
 *  the emoji replaces the lucide shape entirely; `color` keeps applying only
 *  where the default glyph renders, so the two marks never fight. The lucide
 *  shapes carry the collapse cue themselves (Folder vs FolderOpen); the emoji
 *  cannot, so collapsible callers — the ones that pass `open` at all — get a
 *  small disclosure chevron overlaid on the emoji box's bottom-right corner
 *  (the sidebar's one chevron grammar: ChevronRight, rotated 90° when open).
 *  The overlay is absolutely positioned so the glyph box keeps its exact
 *  width and the folder-alignment geometry (ChatSidebar.folderAlignment
 *  .test.tsx) is untouched. Callers that render the glyph outside a
 *  collapse context (modal preview, menus, drag ghost) omit `open` and get
 *  the bare emoji.
 *  Shared by the sidebar rows and the folder-settings modal's live preview. */
export default function FolderGlyph({ color, icon, size = 20, open, className = _FOLDER_GLYPH_CLASS, testId }: { color?: string; icon?: string; size?: number; open?: boolean; className?: string; testId?: string }) {
  const Icon = open ? FolderOpen : Folder
  if (icon) {
    return (
      <span data-testid={testId} aria-hidden className={`relative inline-flex items-center justify-center ${className}`} style={{ width: size, height: size, fontSize: Math.max(12, Math.round(size * 0.85)), lineHeight: 1 }}>
        {icon}
        {open !== undefined && (
          <ChevronRight
            data-testid={testId ? `${testId}-disclosure` : undefined}
            size={Math.max(7, Math.round(size * 0.55))}
            strokeWidth={3}
            className={`absolute -bottom-0.5 -right-1 text-muted transition-transform duration-200 ${open ? 'rotate-90' : ''}`}
          />
        )}
      </span>
    )
  }
  return (
    <span data-testid={testId} aria-hidden className={`relative inline-flex items-center justify-center ${className}`} style={{ width: size, height: size, ...(color ? { color: folderColorStroke(color) } : {}) }}>
      <Icon size={size} strokeWidth={2} fill={open ? 'none' : color ? folderColorWash(color) : 'var(--bg-elevated)'} style={{ transition: 'fill .2s' }} />
    </span>
  )
}
