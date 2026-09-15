import {
  Cloud,
  Flower2,
  Heart,
  Moon,
  Sparkles,
  Star,
  Sun,
  Zap,
  type LucideIcon,
} from 'lucide-react'

/**
 * Stock artwork an installed theme may request for the chat loader.
 *
 * Theme manifests carry names, never components or SVG markup. Keeping this a
 * closed map preserves the installed-theme trust boundary while letting packs
 * reuse Kiro Crew's existing carousel animation.
 */
export const THEME_LOADER_ICONS = {
  cloud: Cloud,
  flower: Flower2,
  heart: Heart,
  moon: Moon,
  sparkles: Sparkles,
  star: Star,
  sun: Sun,
  zap: Zap,
} satisfies Record<string, LucideIcon>

export type ThemeLoaderIconName = keyof typeof THEME_LOADER_ICONS

const MIN_THEME_LOADER_ICONS = 4
const MAX_THEME_LOADER_ICONS = Object.keys(THEME_LOADER_ICONS).length

/** Resolve a complete validated manifest pool, or fail closed to the caller's default. */
export function resolveThemeLoaderIcons(names?: readonly string[]): LucideIcon[] {
  if (
    !names
    || names.length < MIN_THEME_LOADER_ICONS
    || names.length > MAX_THEME_LOADER_ICONS
    || new Set(names).size !== names.length
  ) return []
  const icons = names.map(name => THEME_LOADER_ICONS[name as ThemeLoaderIconName])
  return icons.every((icon): icon is LucideIcon => !!icon) ? icons : []
}
