import { ChevronRight, Folder, FolderOpen } from 'lucide-react'
import { folderColorStroke, folderColorWash, folderDisclosureNotch } from './folderColorPaint'

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
 *  where the default glyph renders, so the two marks never fight. Collapsible
 *  callers — the ones that pass `open` at all — get a small disclosure
 *  chevron overlaid on the glyph box's bottom-right corner (the sidebar's one
 *  chevron grammar: ChevronRight, rotated 90° when open), on the emoji AND on
 *  the lucide shape: first-time users do not read the Folder/FolderOpen swap
 *  as open/closed state and click an expanded folder expecting it to expand
 *  (#11269). The overlay is absolutely positioned so the glyph box keeps its
 *  exact width and the folder-alignment geometry (ChatSidebar.folderAlignment
 *  .test.tsx) is untouched; it sits inside the box's bottom-right corner in a
 *  hole masked out of the shape, so it never crosses the folder outline or
 *  crowds the name. Callers that render the glyph outside a
 *  collapse context (modal preview, menus, drag ghost) omit `open` and get
 *  the bare glyph.
 *  Shared by the sidebar rows and the folder-settings modal's live preview. */
export default function FolderGlyph({ color, icon, size = 20, open, className = _FOLDER_GLYPH_CLASS, testId }: { color?: string; icon?: string; size?: number; open?: boolean; className?: string; testId?: string }) {
  const Icon = open ? FolderOpen : Folder
  const hasDisclosure = open !== undefined
  const notch = folderDisclosureNotch(size)
  const disclosure = hasDisclosure && (
    <ChevronRight
      data-testid={testId ? `${testId}-disclosure` : undefined}
      size={notch.chevron}
      strokeWidth={2.5}
      className={`absolute text-muted transition-transform duration-200 ${open ? 'rotate-90' : ''}`}
      style={{ left: notch.chevronLeft, top: notch.chevronLeft }}
    />
  )
  // The glyph itself is masked with a round hole under the chevron, so the
  // chevron sits in clear space instead of crossing the folder outline (or the
  // emoji). A mask, not a backing disc in the row colour: the row repaints on
  // hover, when pinned and per theme, and a mask is right on all of them.
  const notchMask = hasDisclosure ? { maskImage: notch.maskImage, WebkitMaskImage: notch.maskImage } : undefined
  // A data guard, not defensive noise: folders.json is a hand-editable file on
  // disk, so `icon` is boundary input here even though every server write path
  // coerces it to a string. A non-string value (`{}`, `1`, `["🚀"]`) rendered
  // as a React child throws and takes the whole sidebar down with it; anything
  // that is not a non-empty string falls back to the default lucide glyph.
  if (typeof icon === 'string' && icon) {
    return (
      <span data-testid={testId} aria-hidden className={`relative inline-flex items-center justify-center ${className}`} style={{ width: size, height: size, fontSize: Math.max(12, Math.round(size * 0.85)), lineHeight: 1 }}>
        <span data-testid={testId ? `${testId}-shape` : undefined} className="inline-flex items-center justify-center" style={{ width: size, height: size, ...notchMask }}>{icon}</span>
        {disclosure}
      </span>
    )
  }
  return (
    <span data-testid={testId} aria-hidden className={`relative inline-flex items-center justify-center ${className}`} style={{ width: size, height: size, ...(color ? { color: folderColorStroke(color) } : {}) }}>
      <Icon data-testid={testId ? `${testId}-shape` : undefined} size={size} strokeWidth={2} fill={open ? 'none' : color ? folderColorWash(color) : 'var(--bg-elevated)'} style={{ transition: 'fill .2s', ...notchMask }} />
      {disclosure}
    </span>
  )
}
