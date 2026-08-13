// The Kiro ghost mark, sized and tinted for use as a nav/UI glyph.
//
// The source art (`kiro-ghost-mark.svg`) is the ghost from the Kiro brand mark —
// the same silhouette shipped as the product icon in the public `kirodotdev/Kiro`
// repo (`assets/kiro-icon.png`) and used by the Electron boot sequence
// (`KiroGhost`), traced as an OUTLINE (stroked body, solid eyes) to match how
// the Kiro IDE draws it in its own activity bar. It is a static brand asset
// rendered from a plain URL import — NOT svgr/`?react`, and no inline SVG paths
// in TSX — so it does not fall under the `use-lucide-icons` rule, the same
// carve-out the other brand marks (`components/BrandIcon.tsx`) and the
// onboarding mascot (`assets/onboarding/GhostIcons.tsx`) already rely on.
//
// Rendering is delegated to `BrandGlyph`, the shared masked-brand-glyph span:
// unlike an <img>, it paints the asset as a CSS mask over `currentColor`, so the
// mark inherits the nav rail's colour states (accent when the row is active,
// muted when idle) exactly like the Lucide glyphs it sits beside — and the
// hollow body keeps its visual weight in line with those stroke-drawn glyphs.
import { BrandGlyph } from './BrandIcon'
import ghostMarkUrl from '../assets/kiro-ghost-mark.svg'

/**
 * Kiro ghost brand glyph.
 *
 * @param size  Box edge in px; the ghost is aspect-fit inside it (default 16,
 *              matching the `size={16}` Lucide glyphs in the nav rail).
 */
export function KiroGhostMark({ size = 16, className = 'inline-block shrink-0' }: { size?: number; className?: string }) {
  return <BrandGlyph url={ghostMarkUrl} size={size} className={className} testId="kiro-ghost-mark" />
}

export default KiroGhostMark
