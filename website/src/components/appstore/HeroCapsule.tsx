/**
 * HeroCapsule — the 16:9 app-art thumbnail shared by the store surfaces.
 *
 * Discover's ``AppListRow`` and the Library's ``InstalledAppCard`` render the
 * same app, so they must render the same art with the same fallback chain:
 * theme-appropriate hero (or screenshot) → the app's icon → a name-hashed
 * gradient. Keeping that in one component is what stops the two tabs from
 * drifting apart again — the Library shipped a flat lucide icon for a whole
 * release because the rule lived only inside AppListRow.
 *
 * The two callers differ only in vertical alignment (Discover centers its row,
 * the Library top-aligns), so ``className`` takes the caller's box classes.
 */
import { Package } from 'lucide-react'
import AppIcon from '../AppIcon'
import { gradientFor } from './gradient'
import { useHeroArt } from './useHeroArt'
import type { RegistryApp } from './types'

/** The art-bearing subset of a manifest or registry row. */
export type HeroArtFields = Pick<RegistryApp, 'heroImage' | 'heroImageDark' | 'screenshots'>

export default function HeroCapsule({
  name,
  art,
  icon,
  iconUrl,
  className = 'w-24 h-[54px]',
}: {
  /** App name — seeds the deterministic gradient when there is no art. */
  name: string
  art: HeroArtFields
  icon?: string
  iconUrl?: string
  className?: string
}) {
  const hero = useHeroArt(art)
  const hasIcon = !!(icon || iconUrl)

  return (
    <div
      className={`${className} rounded-lg shrink-0 overflow-hidden grid place-items-center text-white relative`}
      style={hero.src ? { background: 'var(--bg-elevated)' } : { background: gradientFor(name) }}
    >
      {hero.src ? (
        <img src={hero.src} alt="" className="absolute inset-0 w-full h-full object-cover" onError={hero.onError} />
      ) : hasIcon ? (
        <AppIcon icon={icon} iconUrl={iconUrl} size={28} />
      ) : (
        <Package size={22} />
      )}
    </div>
  )
}
