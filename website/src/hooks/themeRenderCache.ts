/**
 * Render cache for the ACTIVE installed theme's detail payload.
 *
 * The server stays the source of truth (same contract as `mc-theme` /
 * `mc-color-theme`): `useTheme` writes the active theme's `CustomThemeData`
 * here after every successful load, and reads it back synchronously at mount so
 * the first paint of a cold load already carries the theme's variables, fonts
 * and branding instead of waiting on the catalog round trips. Consent-bound
 * Level-2 experience content is never applied from this cache; it must come
 * from the current server detail so consent stays bound to the current content.
 * A missing, corrupt or stale entry only means "no cached value" — never an error.
 *
 * Only ONE entry is kept at the fixed key below. Entries over
 * `MAX_ENTRY_CHARS` are not persisted at all so the cache cannot become the
 * thing that fills the origin's storage quota.
 */
import { safeGetItem, safeRemoveItem, safeSetItem } from '../utils/safeStorage'
import type { CustomThemeData } from './useTheme'

export const THEME_DATA_KEY = 'mc-theme-data'
/** Roughly 200 KB of serialized JSON; a real pack detail is a few KB. */
export const MAX_ENTRY_CHARS = 200_000

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function isStringRecord(v: unknown): v is Record<string, string> {
  return isRecord(v) && Object.values(v).every((x) => typeof x === 'string')
}

const optional = (check: (x: unknown) => boolean) => (x: unknown) => x === undefined || check(x)
const isStr = (x: unknown) => typeof x === 'string'
const isNum = (x: unknown) => typeof x === 'number' && Number.isFinite(x)
const isBool = (x: unknown) => typeof x === 'boolean'
const isStrArray = (x: unknown) => Array.isArray(x) && x.every(isStr)
/** Every declared field passes its check; unknown fields are ignored. */
function isShape(v: unknown, fields: Record<string, (x: unknown) => boolean>): boolean {
  if (!isRecord(v)) return false
  // `Object.hasOwn`, not `in`: `in` walks the prototype chain, so a key like
  // `toString` or `__proto__` could satisfy a declared-field lookup.
  return Object.entries(fields).every(([k, ok]) => ok(Object.hasOwn(v, k) ? v[k] : undefined))
}

const isFontFace = (x: unknown) =>
  isShape(x, {
    family: isStr,
    src: isStr,
    weight: optional(isNum),
    style: optional(isStr),
    role: optional(isStr),
  })
/**
 * Exactly the asset fields `renderCacheProjection` keeps. Level-2 fields
 * (overlays, topbar, audio, persona) are not inspected: the projection strips
 * them on both the write and the read path, so a check on them could never
 * change what is seeded.
 */
const isAssets = (x: unknown) =>
  isShape(x, {
    branding: optional((b) =>
      isShape(b, {
        botName: optional(isStr),
        logo: optional(isStr),
        favicon: optional(isStr),
        wordmark: optional(isStr),
      }),
    ),
    fonts: optional((f) => Array.isArray(f) && f.every(isFontFace)),
    hasOverrides: optional(isBool),
    loaderIcons: optional(isStrArray),
    loaderImages: optional(isStrArray),
  })

/**
 * Shape check for a parsed cache entry, covering the fields the Level-1
 * projection keeps — the only fields `writeCachedThemeData` ever persists and
 * `readCachedThemeData` ever seeds. Unknown fields (including any Level-2
 * asset fields from a hand-edited entry) are tolerated so additive server
 * payload changes do not invalidate an otherwise safe cache entry; the
 * projection drops them before the seed.
 */
export function isValidCachedThemeData(v: unknown, slug: string): v is CustomThemeData {
  return isShape(v, {
    slug: (s) => s === slug,
    name: isStr,
    emoji: isStr,
    dark: isStringRecord,
    light: isStringRecord,
    level: optional(isNum),
    assets: optional(isAssets),
  })
}

/**
 * Strip consent-bound Level-2 experience assets from render-cache data.
 * Branding, fonts, overrides metadata, and loader art are safe at first paint;
 * overlays, topbar, audio, and persona data require the current server detail.
 */
export function renderCacheProjection(theme: CustomThemeData): CustomThemeData {
  const { assets, ...rest } = theme
  if (!assets) return { ...rest, level: Math.min(theme.level ?? 0, 1) }

  const projectedAssets: NonNullable<CustomThemeData['assets']> = {}
  const levelOneKeys = [
    'branding',
    'fonts',
    'hasOverrides',
    'loaderIcons',
    'loaderImages',
  ] as const
  for (const key of levelOneKeys) {
    if (Object.hasOwn(assets, key)) {
      Object.assign(projectedAssets, { [key]: assets[key] })
    }
  }
  return {
    ...rest,
    level: Math.min(theme.level ?? 0, 1),
    assets: projectedAssets,
  }
}

/** Read the cached detail for `slug`; a mismatch or corrupt entry is removed. */
export function readCachedThemeData(slug: string): CustomThemeData | null {
  const raw = safeGetItem(THEME_DATA_KEY)
  if (raw === null) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    safeRemoveItem(THEME_DATA_KEY)
    return null
  }
  if (!isRecord(parsed) || parsed.slug !== slug || !isValidCachedThemeData(parsed, slug)) {
    safeRemoveItem(THEME_DATA_KEY)
    return null
  }
  return renderCacheProjection(parsed)
}

/** Persist `theme` as the one cached Level-1 projection. */
export function writeCachedThemeData(theme: CustomThemeData): boolean {
  let serialized: string
  try {
    serialized = JSON.stringify(renderCacheProjection(theme))
  } catch {
    return false
  }
  if (serialized.length > MAX_ENTRY_CHARS) return false
  return safeSetItem(THEME_DATA_KEY, serialized)
}

/** Drop the active-theme render cache. */
export function clearCachedThemeData(): void {
  safeRemoveItem(THEME_DATA_KEY)
}
