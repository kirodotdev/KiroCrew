// The Crew Member mark — two Kiro ghosts, one standing behind the other — sized
// and tinted for use as the `/members` nav glyph.
//
// Why this is an ASSET and not a Lucide glyph: what the rail needs to say here
// is "the named Kiro crew members you hold conversations with", and the half of
// that which carries the meaning is the KIRO GHOST. lucide-react ships no brand
// or mascot marks, so there is nothing to compose the pair from — this is the
// same gap `KiroGhostMark` already fills on this rail, under the
// `use-lucide-icons` BRAND-MARK EXCEPTION (`website/AUTOSDE.yaml`). The layout
// is Lucide's own `Users` idiom (a full figure in front, a partial one behind,
// cut where it is occluded), so the mark reads as "members, plural" the way the
// rest of the icon set says it; the ghost is what makes them Kiro's.
//
// The exception's three conditions are met the way the in-tree precedents meet
// them: the art lives in its own `.svg` under `src/assets/` and is consumed
// through a plain URL import (no `<svg>` element or path data in any `.tsx` —
// the regex gate blocks that unconditionally); it is monochrome, so it is
// painted as a CSS `mask` over `currentColor` via the shared `BrandGlyph`
// helper rather than a fixed-colour `<img>`, which is what lets it follow the
// rail's active (accent) / idle (muted) colour states and every theme's palette;
// and the identity it depicts is the Kiro ghost.
//
// GHOST GEOMETRY: this is a SIMPLIFIED ghost (asymmetric dome, taller right
// shoulder, flared bottom-left tail, two-bump hem), not the full brand outline.
// The full outline's three-lobe hem and inner tail curl were tried first and
// turn to mush at the rail's 16px on a 1x display; the simplification keeps the
// silhouette cues that survive that size.
//
// STROKE WEIGHT: the asset is drawn on a 24×24 viewBox at `stroke-width="2"` —
// byte-for-byte Lucide's own geometry contract — so at the rail's `size={16}` it
// scales to the same 1.33px optical stroke as the `size={16}` Lucide glyphs it
// sits beside. `test/CrewMemberMark.test.tsx` pins both numbers in the asset,
// because editing either one silently makes this glyph heavier or lighter than
// every one of its neighbours and nothing else in the build would notice.
import { BrandGlyph } from './BrandIcon'
import crewMemberMarkUrl from '../assets/crew-member-mark.svg'

/**
 * Crew Member brand glyph — the two-ghost pair.
 *
 * @param size  Box edge in px; the mark is aspect-fit inside it (default 16,
 *              matching the `size={16}` Lucide glyphs in the nav rail).
 */
export function CrewMemberMark({ size = 16, className = 'inline-block shrink-0' }: { size?: number; className?: string }) {
  return <BrandGlyph url={crewMemberMarkUrl} size={size} className={className} testId="crew-member-mark" />
}
