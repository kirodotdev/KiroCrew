/**
 * AppIconTile — the square icon thumbnail shared by the store's LIST surfaces.
 *
 * Discover's ``AppListRow`` and the Library's ``InstalledAppCard`` render the
 * same app, so they must render the same thumbnail with the same fallback chain:
 * the app's theme-appropriate icon → a name-hashed gradient carrying a glyph.
 * Keeping that in one component is what stops the two tabs from drifting apart
 * — the Library shipped a flat lucide icon for a whole release because the rule
 * lived only inside AppListRow.
 *
 * WHY A LIST ROW SHOWS AN ICON AND NOT HERO ART. Hero art is EDITORIAL: it is
 * the argument for one app, and it earns its space on the spotlight and the
 * feature cards, where there are two or three of them and each gets a wide
 * panel. A list is the opposite job — it is scanned, and the reader is matching
 * a name to a mark they already recognise. A 16:9 crop of someone's marketing
 * art at 96x54 is neither: too small to read as art, too large to scan as an
 * identity, and it makes twelve rows look like twelve billboards. The icon is
 * the app's identity at any size, which is what a list needs.
 *
 * That split also means the two kinds of image now have exactly one owner each:
 * ``useHeroArt`` is reached only from the editorial surfaces, and this component
 * is the only thumbnail a list renders.
 *
 * The two callers differ only in vertical alignment (Discover centers its row,
 * the Library top-aligns), so ``className`` takes the caller's box classes.
 */
import { Package } from 'lucide-react'
import AppIcon from '../AppIcon'
import { gradientFor } from './gradient'

export default function AppIconTile({
  name,
  icon,
  iconUrl,
  iconUrlDark,
  className = 'w-11 h-11',
}: {
  /** App name — seeds the deterministic gradient when there is no icon. */
  name: string
  icon?: string
  iconUrl?: string
  iconUrlDark?: string
  className?: string
}) {
  const hasIcon = !!(icon || iconUrl || iconUrlDark)

  return (
    <div
      className={`${className} rounded-lg shrink-0 overflow-hidden grid place-items-center text-white relative border border-border`}
      // An app that ships an opaque icon covers this tile, so the gradient is
      // only ever seen in the no-icon case. Seeding it from the name keeps a
      // given app the same colour on every surface and across reloads, which is
      // what makes it read as identity rather than decoration.
      //
      // The plate under an icon is not decoration either: a light opaque icon on
      // light chrome has no edge of its own, and without it the mark dissolves
      // into the card and the row reads as a stray letter. The wide art capsule
      // this replaced got that boundary for free from the image.
      style={hasIcon ? { background: 'var(--bg-elevated)' } : { background: gradientFor(name) }}
    >
      {hasIcon ? (
        <AppIcon icon={icon} iconUrl={iconUrl} iconUrlDark={iconUrlDark} size={30} />
      ) : (
        <Package size={22} />
      )}
    </div>
  )
}
