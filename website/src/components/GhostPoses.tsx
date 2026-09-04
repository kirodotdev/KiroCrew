/**
 * Kiro ghost poses — the 8 mascot poses from the Kiro Design System
 * ("More Ghost Pose", Figma node 422:734), used by the chat loading carousel.
 *
 * The source art lives in `assets/ghost-poses/pose-N.svg` and is rendered through
 * a plain `<img>` via a default URL import — NOT svgr/`?react`, and with no `<svg>`
 * element or path data in this (or any) TSX file. That is what the `use-lucide-icons`
 * rule requires of a brand mark: lucide-react ships no mascot glyphs, so the art is
 * exempt from "use lucide-react" ONLY while it stays an asset rather than inline
 * code. Same pattern as `assets/onboarding/GhostIcons.tsx`.
 *
 * The asset is a fixed white-body / black-eyes silhouette; theming is applied to the
 * `<img>` from CSS (see `.kpc` in index.css), where the light palette traces a black
 * outline with a `drop-shadow()` filter chain so the white body reads against a pale
 * surface. A CSS filter follows the image's rendered alpha, so no per-theme asset
 * variant is needed.
 */
import pose1 from '../assets/ghost-poses/pose-1.svg'
import pose2 from '../assets/ghost-poses/pose-2.svg'
import pose3 from '../assets/ghost-poses/pose-3.svg'
import pose4 from '../assets/ghost-poses/pose-4.svg'
import pose5 from '../assets/ghost-poses/pose-5.svg'
import pose6 from '../assets/ghost-poses/pose-6.svg'
import pose7 from '../assets/ghost-poses/pose-7.svg'
import pose8 from '../assets/ghost-poses/pose-8.svg'

/** Pose art URLs, in design-system order. */
export const GHOST_POSE_URLS: string[] = [pose1, pose2, pose3, pose4, pose5, pose6, pose7, pose8]

/** One pose, sized by the carousel. Decorative: no alt text, aria-hidden. */
export function GhostPose({ src }: { src: string }) {
  return <img className="kp" src={src} alt="" aria-hidden="true" draggable={false} />
}

/**
 * The poses as loader-carousel icons — the shape `ThemeBranding.loaderIcons`
 * expects. Safe to swap between: the carousel animates a persistent wrapper, so
 * remounting the inner element does not restart the cross-fade.
 */
export const GHOST_POSE_ICONS = GHOST_POSE_URLS.map(
  src => function GhostPoseIcon() { return <GhostPose src={src} /> }
)
