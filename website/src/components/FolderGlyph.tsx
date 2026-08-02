import { FOLDER_BACK_FILL, folderBackMasks, folderColorBackFill, folderColorFrontFill, folderColorStroke, folderFrontOutlinePx } from './folderGlyphMasks'

// Default classes carry the stroke color: panels paint via currentColor (the
// back's line layer and the front's border both read it), so the root's text
// color IS the outline color — muted/70 at rest like the resting rail icons,
// stepping up to full muted when the row (a `group`) is hovered. Callers that
// pass className own the color instead.
const _FOLDER_GLYPH_CLASS = 'shrink-0 text-muted/70 group-hover:text-muted transition-colors'

/** The sidebar folder glyph: a two-panel hand-drawn folder (mask-painted back
 *  silhouette + bordered front face) with a duo-tilt open animation, speaking
 *  lucide's proportional-stroke language. Shared by the sidebar rows and the
 *  folder-settings modal's live preview. */
export default function FolderGlyph({ icon, color, size = 20, open = false, className = _FOLDER_GLYPH_CLASS, testId }: { icon?: string; color?: string; size?: number; open?: boolean; className?: string; testId?: string }) {
  const h = Math.round(size * 0.875)
  const frontPx = folderFrontOutlinePx(size)
  // A folder's identity mark is its palette color. Stroke: the color
  // darkened toward text-strong so linework keeps rail-icon contrast; faces:
  // a light wash (10% back / 18% front) over the same surface gray. The
  // inline `color` wins over the rest/hover classes, so colored folders hold
  // their color on hover. The `icon` prop carries the folder's stored icon
  // value (a data-model field the glyph does not render).
  void icon
  const stroke = color ? folderColorStroke(color) : undefined
  const backFill = color ? folderColorBackFill(color) : FOLDER_BACK_FILL
  const frontFill = color ? folderColorFrontFill(color) : FOLDER_BACK_FILL
  const ease = 'transform .22s ease'
  return (
    <span data-testid={testId} aria-hidden className={`relative inline-flex ${className}`} style={{ width: size, height: h, ...(stroke ? { color: stroke } : {}) }}>
      {/* back group — the classic continuous silhouette (tab → S-bend →
       *  raised body edge) painted through two masked layers: one fills the
       *  shape, one strokes its perimeter at constant width. Closed: only the
       *  strip above the front (tab + slope + body edge) is visible. Open:
       *  the group tilts left (+8°) and the body's stroked edges emerge from
       *  behind the front (the folder mouth). */}
      {(() => {
        const masks = folderBackMasks(size, h)
        return (
          <span className="absolute inset-0" style={{ transform: open ? 'skewX(8deg)' : 'none', transformOrigin: '50% 100%', transition: ease }}>
            {/* fill layer; its mask ALSO clips the child stroke layer, cutting
             *  off the stroke's outer half — an SVG stroke straddles the path
             *  edge, so unclipped it renders at 2× the front's border width
             *  with a ragged outer edge. Nested, the visible outline is
             *  inside-aligned at the nominal outline width and hugs the
             *  silhouette. */}
            <span className="absolute inset-0" style={{ background: backFill, WebkitMaskImage: masks.fill, maskImage: masks.fill, WebkitMaskRepeat: 'no-repeat', maskRepeat: 'no-repeat' }}>
              <span className="absolute inset-0" style={{ background: 'currentColor', WebkitMaskImage: masks.line, maskImage: masks.line, WebkitMaskRepeat: 'no-repeat', maskRepeat: 'no-repeat' }} />
            </span>
          </span>
        )
      })()}
      {/* front panel — outlined rect covering the body up to just under the
       *  tab. Open tilts it right (−11°) with a squash, so the duo-tilt
       *  splits the motion. */}
      <span className="absolute flex items-center justify-center" style={{ left: '2%', right: '2%', top: '22%', bottom: '2%', borderRadius: '12% / 16%', background: frontFill, border: `${frontPx}px solid currentColor`, transform: open ? 'skewX(-11deg) scaleY(.9)' : 'none', transformOrigin: '50% 100%', transition: ease }}>
      </span>
    </span>
  )
}
