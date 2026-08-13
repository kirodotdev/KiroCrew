/**
 * Deterministic avatar for a crew.
 *
 * The seed is the crew name, so a crew keeps the same face forever and two
 * people looking at the same config see the same roster. Generation is fully
 * LOCAL — `@dicebear/core` renders the SVG in-process from the `kiroGhost` style
 * definition. Nothing is fetched, so this works offline and no crew name ever
 * leaves the machine (DiceBear's HTTP API is deliberately not used).
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
import { useMemo } from 'react'
import { createAvatar } from '@dicebear/core'
import { kiroGhost } from '../lib/kiroGhostAvatar'

/** Kiro's own ghost, built on the shipped mark. See `lib/kiroGhostAvatar.ts`. */
const STYLE = kiroGhost

/**
 * Generated data URIs, keyed by seed. Module-level rather than per-component
 * so a crew's avatar is generated once per session even though it is rendered
 * in both the roster card and the editor panel.
 */
const CACHE = new Map<string, string>()

export interface CrewAvatarProps {
  /** Crew name — the whole identity of the image. */
  seed: string
  /** Rendered edge length in px. */
  size?: number
  className?: string
}

export default function CrewAvatar({ seed, size = 40, className = '' }: CrewAvatarProps) {
  const src = useMemo(() => {
    const hit = CACHE.get(seed)
    if (hit) return hit
    // The tile color is part of the style rather than a `backgroundColor` list,
    // so that it is drawn from the same seeded stream as every other trait.
    const uri = createAvatar(STYLE, { seed, radius: 12 }).toDataUri()
    CACHE.set(seed, uri)
    return uri
  }, [seed])

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
