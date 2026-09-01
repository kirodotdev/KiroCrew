/**
 * Deterministic avatar for a crew, with an optional per-crew override.
 *
 * By default the seed is the crew name, so a crew keeps the same face forever
 * and two people looking at the same config see the same roster. A crew record
 * may instead pin explicit ghost traits (`avatar: {kind:'ghost', traits}`,
 * authored in the avatar builder); the pinned face is composed through the
 * same style module, so preview === roster === editor. Generation is fully
 * LOCAL — `@dicebear/core` renders the SVG in-process from the `kiroGhost`
 * style definition. Nothing is fetched, so this works offline and no crew name
 * ever leaves the machine (DiceBear's HTTP API is deliberately not used).
 *
 * Rendered as an `<img>` carrying a data URI rather than inlined SVG markup.
 * Two reasons, both load-bearing:
 *  - no `dangerouslySetInnerHTML`, so this stays clear of the frontend-security
 *    rule and there is no HTML-string path to audit;
 *  - inline DiceBear SVGs collide on their internal `id`s when several are on
 *    one page (clip paths resolve to whichever came first, which renders some
 *    styles blank). A data URI is its own document, so the problem cannot
 *    arise and `randomizeIds` is unnecessary.
 *
 * Swapping the art set is a one-line change to STYLE below; nothing outside
 * this file knows which style is in use.
 */
import { useMemo, useState } from 'react'
import { createAvatar } from '@dicebear/core'
import {
  BRAND_PURPLE,
  GHOST_RADIUS_PCT,
  ghostDataUri,
  kiroGhost,
  type KiroGhostTraits,
  type WorkingIntensity,
} from '../lib/kiroGhostAvatar'

/** Kiro's own ghost, built on the shipped mark. See `lib/kiroGhostAvatar.ts`. */
const STYLE = kiroGhost

/** The stored per-crew override, as the backend round-trips it.
 *
 * `image` marks an uploaded picture served from the per-crew avatar endpoint;
 * `v` is the upload's cache-busting stamp. `pendingData` and `promote` never
 * persist: `pendingData` is the editor draft's not-yet-uploaded picture (a
 * data URI), and `promote` is the wire-only Save directive that tells the
 * server "this save just staged a fresh upload — commit it" (without it, a
 * leftover staging from an abandoned save must not ride into an unrelated
 * edit). */
export type CrewAvatarOverride =
  | { kind: 'ghost'; traits: KiroGhostTraits }
  | { kind: 'image'; v?: number; file?: string; pendingData?: string; promote?: boolean; token?: string }

const TILE_RE = /^#[0-9a-f]{6}$/

/**
 * Interpret a crew record's `avatar` field as an uploaded-picture override.
 * Returns the image descriptor, or `null` when the field is absent, junk, or
 * a ghost override. Total for the same reason as `ghostTraitsFrom`: roster
 * rows carry the field untyped.
 */
export function imageAvatarFrom(
  avatar: unknown,
): { v?: number; pendingData?: string } | null {
  if (!avatar || typeof avatar !== 'object') return null
  const a = avatar as Record<string, unknown>
  if (a.kind !== 'image') return null
  return {
    v: typeof a.v === 'number' ? a.v : undefined,
    pendingData: typeof a.pendingData === 'string' ? a.pendingData : undefined,
  }
}

/**
 * Interpret a crew record's `avatar` field. Returns the pinned traits, or
 * `null` for "no override" (absent, `{}`, or junk — the backend collapses
 * junk to `{}`, but this stays total because MembersPage rows carry the field
 * untyped). Unknown trait options are kept verbatim: `compose` resolves them
 * to "absent" (`EYES[k] ?? ''`), which is the same forgiveness the backend
 * applies, so an old client renders a face saved by a newer vocabulary
 * without crashing.
 */
export function ghostTraitsFrom(avatar: unknown): KiroGhostTraits | null {
  if (!avatar || typeof avatar !== 'object') return null
  const a = avatar as Record<string, unknown>
  if (a.kind !== 'ghost' || !a.traits || typeof a.traits !== 'object') return null
  const t = a.traits as Record<string, unknown>
  const s = (k: string) => (typeof t[k] === 'string' ? (t[k] as string) : '')
  const tile = s('tile')
  return {
    eyes: s('eyes'),
    brows: s('brows'),
    mouth: s('mouth'),
    accessory: s('accessory'),
    prop: s('prop'),
    blush: !!t.blush,
    flip: !!t.flip,
    // The tile is interpolated into SVG markup, so anything but a hex color
    // falls back to the brand tile rather than reaching the string template.
    tile: TILE_RE.test(tile) ? tile : BRAND_PURPLE,
  }
}

/**
 * Generated data URIs for NAME-SEEDED avatars only, keyed by seed + working
 * intensity (NUL-joined so the parts cannot collide with a seed containing
 * the tier word). Module-level rather than per-component so a crew's avatar
 * is generated once per session even though it is rendered in both the
 * roster card and the editor panel; a crew has at most three entries (still,
 * subtle, full). Pinned-trait faces are deliberately NOT cached here: the
 * builder generates a fresh trait combination on every picker click, so a
 * trait-keyed entry would accumulate one encoded SVG per click for the life
 * of the tab. The component's own useMemo covers the pinned path.
 */
const CACHE = new Map<string, string>()

export interface CrewAvatarProps {
  /** Crew name — the identity of the image when no override is pinned. */
  seed: string
  /**
   * The crew record's `avatar` field, verbatim. Accepted untyped because some
   * surfaces (member roster rows) carry the backend dataclass loosely; the
   * coercion lives here so call sites stay one-liners.
   */
  avatar?: unknown
  /** Rendered edge length in px. */
  size?: number
  /** Animate the ghost as "at work". `subtle` for dense lists, `full` for a
   *  single-avatar surface. Identity is untouched — the working variant only
   *  moves what the face drew — so omitting it is a lossless still frame. */
  working?: WorkingIntensity
  /** Fired when an uploaded picture fails to load (before the seeded-ghost
   *  fallback renders). Surfaces the failure where a bare fallback would
   *  read as "saved fine" — the editor shows an inline warning through it. */
  onImageError?: () => void
  className?: string
}

export default function CrewAvatar({
  seed,
  avatar,
  size = 40,
  working,
  onImageError,
  className = '',
}: CrewAvatarProps) {
  const traits = useMemo(() => ghostTraitsFrom(avatar), [avatar])
  const image = useMemo(() => imageAvatarFrom(avatar), [avatar])
  // An uploaded picture that fails to load (file deleted out-of-band, stale
  // record) falls back to the name-derived ghost rather than the browser's
  // broken-image glyph. Keyed by the src so a REPLACED picture gets a fresh
  // chance instead of inheriting the previous file's failure.
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  const src = useMemo(() => {
    // Pinned traits render fresh (see the CACHE comment); useMemo already
    // dedupes re-renders of one mounted instance. `working` is a render
    // parameter of the same compose() path, so a customized face animates
    // exactly like a seeded one.
    if (traits) return ghostDataUri(traits, working)
    const key = [seed, working ?? ''].join('\u0000')
    const hit = CACHE.get(key)
    if (hit) return hit
    // The tile color is part of the style rather than a `backgroundColor` list,
    // so that it is drawn from the same seeded stream as every other trait.
    const uri = createAvatar(STYLE, { seed, radius: GHOST_RADIUS_PCT, working }).toDataUri()
    CACHE.set(key, uri)
    return uri
  }, [seed, traits, working])

  // The editor draft's not-yet-uploaded picture previews directly; a saved
  // one is served by the authenticated API (same-origin cookie auth), with
  // the upload stamp as the cache-buster so a replaced face shows up without
  // waiting out the browser cache.
  const imageSrc = image
    ? (image.pendingData ??
      `/api/agents/${encodeURIComponent(seed)}/avatar${image.v ? `?v=${image.v}` : ''}`)
    : null

  if (imageSrc && failedSrc !== imageSrc) {
    return (
      <img
        src={imageSrc}
        alt=""
        aria-hidden="true"
        width={size}
        height={size}
        style={{ width: size, height: size }}
        onError={() => {
          setFailedSrc(imageSrc)
          onImageError?.()
        }}
        // object-cover: the client crops square before upload, but an old or
        // hand-placed file may not be — cover keeps the tile's rhythm either way.
        className={`shrink-0 rounded-md border border-border bg-bg-elevated object-cover ${className}`}
      />
    )
  }

  return (
    <img
      src={src}
      // Decorative: the crew name is always rendered as text next to it, so
      // announcing the avatar too would just repeat it.
      alt=""
      aria-hidden="true"
      width={size}
      height={size}
      style={{ width: size, height: size }}
      className={`shrink-0 rounded-md border border-border bg-bg-elevated ${className}`}
    />
  )
}

/** The name-derived traits for a seed — the builder's pre-fill and its
 *  "reset to default" preview. Same draw the roster made, read back out. */
export function seededTraits(seed: string): KiroGhostTraits {
  const extra = createAvatar(STYLE, { seed, radius: GHOST_RADIUS_PCT }).toJson().extra
  return ghostTraitsFrom({ kind: 'ghost', traits: extra }) as KiroGhostTraits
}
