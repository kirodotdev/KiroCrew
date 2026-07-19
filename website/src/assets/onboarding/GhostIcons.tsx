// Onboarding mascot icons. The source art (kiro-ghost-var1.svg / -var2.svg) is
// a static, non-icon illustration asset rendered via a plain <img> (default URL
// import — NOT svgr/`?react`, and no inline SVG paths in TSX), so it doesn't
// fall under the `use-lucide-icons` rule.
//
// Live theming is preserved WITHOUT touching the SVG internals: the accent
// outline is a CSS `drop-shadow(... var(--accent))` filter applied to the <img>
// element. A CSS filter traces the image's rendered alpha and reads the theme
// accent from the cascade, so the outline re-skins live even though the asset
// itself is a fixed white-body / black-eyes silhouette.
import ghostVar1Url from './kiro-ghost-var1.svg'
import ghostVar2Url from './kiro-ghost-var2.svg'

// 8-way accent outline: offset drop-shadows every 45° at a ~0.9px radius (CSS
// +Y is down). The up-left offset is trimmed further because the ghost's broad
// top-left dome otherwise reads thicker there. Traces the silhouette in the
// theme accent (`var(--accent)`) so it re-skins live. A soft glow trails it.
const OUTLINE = [
  [1.0, 0], [1.1, 1.1], [0, 0.9], [-0.64, 0.64],
  [-0.72, 0], [-0.28, -0.28], [0, -0.7], [0.64, -0.64],
]
  .map(([x, y]) => `drop-shadow(${x}px ${y}px 0 var(--accent))`)
  .join(' ')
const GLOW = 'drop-shadow(0 6px 14px var(--accent-glow))'
const themedStyle = { display: 'block', filter: `${OUTLINE} ${GLOW}`, transform: 'translateY(-2px)' } as const

/** Step 1 (Pick your look) mascot — intrinsic 51×48. */
export function GhostVar1({ width = 52 }: { width?: number }) {
  return (
    <img
      src={ghostVar1Url}
      width={width}
      height={(width * 48) / 51}
      alt=""
      aria-hidden="true"
      style={themedStyle}
    />
  )
}

/** Steps 2-4 (Schedule / Apps / Sessions) mascot — intrinsic 61×48. */
export function GhostVar2({ width = 44 }: { width?: number }) {
  return (
    <img
      src={ghostVar2Url}
      width={width}
      height={(width * 48) / 61}
      alt=""
      aria-hidden="true"
      style={themedStyle}
    />
  )
}
