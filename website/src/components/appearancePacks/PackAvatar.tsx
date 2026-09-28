/**
 * A crew's face when the crew wears an APPEARANCE PACK — art somebody else drew,
 * served per state from the crew appearance library.
 *
 * `CrewAvatar` composes the seeded ghost itself and draws an uploaded picture as
 * an `<img>`. A pack is neither: its art may be an SVG, a Lottie document or a
 * row of a sprite sheet, and the format is a property of the SLOT rather than of
 * the pack, so which player draws it can only be known after reading the pack.
 * That read is what this component owns, and it is why the pack tier is a
 * component rather than another `src` in `CrewAvatar`.
 *
 * Three tiers, one per format:
 *   svg     — an `<img>` pointed at the per-slot route, exactly as before.
 *   lottie  — `LottieRenderer`, looping.
 *   sprite  — `SpriteRenderer`, stepping the row this slot occupies.
 *
 * THE TWO PLAYERS ARE BOUNDED BY VISIBILITY AND BY STATE. A roster draws one
 * avatar per crew at 18-38px, so a page can hold dozens; a Lottie timeline or a
 * sprite loop per row would spend real per-frame work on faces scrolled far out
 * of view, and a dozen looping idles make motion the wallpaper of the page. Each
 * avatar therefore observes its own box and plays only while it intersects the
 * viewport AND the state is a reaction — off screen, or at `idle`, it holds frame
 * 0. The rule is visibility rather than a size threshold on purpose: the dense
 * roster's own avatars are 38px, so any threshold low enough to animate the crew
 * card would animate every row in the list at once, which is the cost this bound
 * exists to remove. `prefers-reduced-motion` holds both players on their first
 * frame regardless: they are JS-driven timelines, so the stylesheet's global
 * reduced-motion rule cannot reach them, and the preference is read here.
 *
 * The svg tier takes none of this, deliberately: it is a plain `<img>` on the
 * slot route — the same element the picture tier already is — and a document
 * inside an `<img>` is reachable by neither the page's stylesheet nor a flag on
 * this component. A pack SVG that animates itself (SMIL) animates as any image
 * does. Bounding it would mean fetching and inlining every svg slot as a
 * document, which is the cost the `<img>` was chosen to avoid.
 *
 * A pack that cannot be drawn — the read failed after the shared retry, the
 * renderer refused the art, or no slot resolves for this state — reports through
 * `onError` and renders the caller's `fallback` IN PLACE, staying mounted. That
 * is what keeps the query observed: a query left in error state has no data and
 * is refetched on the next focus, reconnect or invalidation, and because this
 * component is still here to receive the answer, a gateway blip is not a
 * permanent ghost. Unmounting on failure (which a caller's own latch would do)
 * drops the observer, and the refetch has nobody to draw for.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { LottieRenderer } from './LottieRenderer'
import { SpriteRenderer } from './SpriteRenderer'
import { packSlotUrl } from '../../lib/appearancePacks/library'
import { resolveSlot, spriteRowFor, type PackDetail } from '../../lib/appearancePacks/detail'
import { usePackDetail } from '../../hooks/usePackDetail'
import { useReducedMotion } from '../../hooks/useReducedMotion'

export interface PackAvatarProps {
  /** The pack id from the crew's `avatar` record — already validated by
   *  `packAvatarFrom`, and never the built-in `kiro-ghost` (whose art ships in
   *  this bundle and is composed locally). */
  id: string
  /** Which reaction to draw. Resolved through the pack's own fallback chain, so
   *  a pack that draws only `idle` still answers every state. */
  state: string
  /** Rendered edge length in px. The art fits this box. */
  size: number
  className?: string
  /** Fired ONCE each time the pack goes from drawable to not — it cannot be
   *  read, the renderer refused its art, or it draws nothing for any state — so
   *  a blank face is never reported as saved fine. Not re-fired for a re-render
   *  while still failed, and fired again after a recovery that fails anew. */
  onError?: () => void
  /** What to draw while the pack cannot be: `CrewAvatar` hands in the seeded
   *  ghost. Rendered in place, with this component still mounted, so the pack's
   *  query keeps its observer and a later refetch redraws the art. Absent means
   *  draw nothing, which is what the Library tab wants — it marks the card. */
  fallback?: ReactNode
}

export default function PackAvatar({ id, state, size, className = '', onError, fallback }: PackAvatarProps) {
  // One read per pack per session, shared across every avatar wearing it, retried
  // once, and re-read for every mounted subscriber when the Library invalidates
  // the key. React Query owns all of that; this component only asks.
  const { data: detail, isError: readFailed } = usePackDetail(id)
  /** The RENDERER could not draw the art it was handed (broken image, malformed
   *  clip, sheet without the row). Distinct from the read failing. Recorded
   *  AGAINST the art it refused — the id and the detail object — so a different
   *  pack, or a re-read that produced different art (a re-import under the same
   *  id, a recovery after a blip), is tried afresh rather than inheriting the
   *  verdict, while identical art (React Query hands back the same object for
   *  unchanged data) stays refused: same bytes, same answer. Keyed rather than
   *  reset in an effect because a renderer refuses in ITS mount effect, which
   *  runs before this component's effects in the same commit — a reset keyed on
   *  the detail arriving would then land after the refusal and erase it. */
  const [refused, setRefused] = useState<{ id: string; detail: PackDetail } | null>(null)
  const failed = refused !== null && refused.id === id && refused.detail === detail
  const [visible, setVisible] = useState(false)
  const reducedMotion = useReducedMotion()
  // Animate only when on screen, when the user has not asked for less motion,
  // AND when the state is a reaction. `idle` holds its first frame: a roster
  // holds dozens of these faces, and a dozen looping idles make motion the
  // wallpaper of the page, so motion is reserved for something happening —
  // a turn running, a turn done, an error — which is what a reaction is.
  const playing = visible && !reducedMotion && state !== 'idle'

  // The observer follows the NODE, not the mount: a ref callback rather than a
  // mount-once effect, because the box is unmounted while the fallback shows
  // (the fallback is the caller's element, not this span) and a recovery mounts
  // a fresh span. An effect keyed on `[]` had observed the first span only, so
  // a recovered avatar kept whatever `visible` last said — playing off screen,
  // or still on it. The callback runs with the new node on mount and with
  // `null` on unmount, and swaps the observer each time.
  const observerRef = useRef<IntersectionObserver | null>(null)
  const observeBox = useCallback((node: HTMLSpanElement | null) => {
    observerRef.current?.disconnect()
    observerRef.current = null
    if (!node) return
    // No IntersectionObserver (an old engine, a test env that removed it) means
    // no visibility signal, so animate: a still avatar everywhere is a worse
    // regression than an unbounded one on an engine nobody ships.
    if (typeof IntersectionObserver === 'undefined') {
      setVisible(true)
      return
    }
    const observer = new IntersectionObserver((entries) => {
      // Latest entry only. A burst of records for one target is a scroll, and
      // the last one is where it came to rest.
      const last = entries[entries.length - 1]
      if (last) setVisible(last.isIntersecting)
    })
    observer.observe(node)
    observerRef.current = observer
  }, [])

  // A pack that draws NOTHING for this state even after fallback is as broken as
  // one that could not be read — both leave the crew faceless, so both take the
  // ghost. Resolved before the error report below so one effect covers both.
  const slot = useMemo(() => (detail ? resolveSlot(detail, state) : null), [detail, state])
  const broken = failed || readFailed || (detail !== undefined && slot === null)

  // Reported from an effect rather than during render: `onError` is a caller's
  // state write (the crew editor shows a warning, the Library marks the card),
  // and doing that in a render body updates another component mid-render. Fired
  // on the EDGE only: this component stays mounted while broken, so a caller
  // re-rendering with a fresh `onError` identity must not be told again.
  const wasBroken = useRef(false)
  useEffect(() => {
    if (broken && !wasBroken.current) onError?.()
    wasBroken.current = broken
  }, [broken, onError])

  // Stable for one pack's art, because `LottieRenderer` takes it as an effect
  // dependency: a fresh identity per render would destroy and reload the
  // animation every render. It changes with the art, which reloads anyway.
  const reportFailed = useCallback(() => {
    if (detail) setRefused({ id, detail })
  }, [id, detail])

  const box = `shrink-0 overflow-hidden rounded-md border border-border bg-bg-elevated ${className}`

  // In place and still mounted — see the header for why unmounting here would
  // make every read failure permanent.
  if (broken) return <>{fallback ?? null}</>

  // Before the read lands there is nothing to draw and no way to know which
  // player will draw it, so the box holds its space rather than flashing a ghost
  // that the art then replaces. (A read that FAILED is `broken` above, so this is
  // only ever the pending state.)
  if (!detail || !slot) {
    return (
      <span
        ref={observeBox}
        aria-hidden="true"
        // `.skeleton` shimmers, so a slow or dead read reads as loading rather
        // than as a blank face for the length of the retry window.
        className={`${box} skeleton`}
        style={{ width: size, height: size, display: 'inline-block' }}
        data-testid="pack-avatar-pending"
      />
    )
  }

  const art = detail.animations[slot]

  return (
    <span
      ref={observeBox}
      aria-hidden="true"
      className={box}
      style={{ width: size, height: size, display: 'inline-block', lineHeight: 0 }}
      data-testid={`pack-avatar-${art.format}`}
      data-pack-slot={slot}
      data-pack-playing={playing ? 'true' : 'false'}
    >
      {art.format === 'lottie' ? (
        <LottieRenderer
          animationData={art.content}
          width={size}
          height={size}
          loop
          autoplay={playing}
          // Malformed art is a load failure like a missing file: the importer only
          // checks that a pack's `.json` is non-empty, so a clip this player
          // cannot parse reaches here and must fall back rather than draw nothing.
          onError={reportFailed}
        />
      ) : art.format === 'sprite' ? (
        <SpriteRenderer
          // The sheet as an IMAGE, from the slot route — which base64-decodes it
          // to `image/png`. The inlined `content` is base64 text, and a canvas
          // cannot draw text.
          src={packSlotUrl(id, slot)}
          // Each of these is a positive finite number or absent — `packDetailFrom`
          // drops anything else — so a served `"0"` cannot reach the renderer's
          // frame count as `Infinity`. Absent falls to the avatar's own box for a
          // dimension and to SpriteRenderer's default for the rate.
          frameWidth={detail.sprite?.frameWidth ?? size}
          frameHeight={detail.sprite?.frameHeight ?? size}
          fps={detail.sprite?.fps}
          displaySize={size}
          row={spriteRowFor(detail, slot)}
          playing={playing}
          // A sheet that will not decode is a load failure like a missing file:
          // report it so the crew gets the ghost rather than a transparent tile.
          onError={reportFailed}
        />
      ) : (
        <img
          // The per-slot route, not the inlined `content`: it carries the
          // inert-SVG content policy the detail route's JSON body does not, and
          // it is what the picker's thumbnails already request, so the browser
          // cache is shared with them.
          src={packSlotUrl(id, slot)}
          alt=""
          aria-hidden="true"
          width={size}
          height={size}
          // object-contain, not cover: a pack's art is drawn to its own frame and
          // cropping it would cut the character's head off. The picture tier
          // crops because the client squares an upload before sending it.
          style={{ width: size, height: size, objectFit: 'contain' }}
          onError={reportFailed}
        />
      )}
    </span>
  )
}
