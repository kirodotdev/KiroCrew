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
 * A crew may instead wear an APPEARANCE PACK (`avatar: {kind:'pack', id}`),
 * whose art is drawn by somebody else and served from the crew appearance
 * library. Any format the library holds renders: `PackAvatar` reads the pack,
 * picks a player per slot — an `<img>` for SVG, `LottieRenderer` for a Lottie
 * document, `SpriteRenderer` for a row of a sprite sheet — and bounds animation
 * to the avatars the viewport actually shows. The built-in `kiro-ghost` pack is
 * the seeded ghost itself and is composed locally.
 *
 * On top of that identity sits a REACTION layer, and it belongs to the ghost
 * alone: `state` picks one of `working` / `done` / `error`, and a ghost plays a
 * built-in MOTION for `done` and `error` — the record's `motions` map names it,
 * or the default does. Identity is never part of it — see
 * `lib/crewAvatarState.ts` — so a reacting crew still reads as the same crew.
 * A picture is static, and a pack brings its own per-state art; for both,
 * `state` only picks which drawing is served.
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
import {
  motionFor,
  motionsFrom,
  soundsFrom,
  type AvatarFaceState,
  type AvatarMotions,
  type AvatarSounds,
} from '../lib/crewAvatarState'
import { BUILTIN_PACK_ID } from '../lib/appearancePacks/library'
import PackAvatar from './appearancePacks/PackAvatar'

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
 * edit).
 *
 * `pack` names an appearance pack from the crew appearance library; the art is
 * served per state from `GET /api/appearances/{id}/slot/{slot}`. Nothing about
 * the pack is copied into the record — `id` is the whole of it.
 *
 * `traits` is OPTIONAL on the ghost tier: `{kind:'ghost', motions}` with no
 * traits is a valid record and means "the name-derived face, plus these
 * reactions". The reaction keys are the GHOST's alone — a picture is static and
 * silent, and a pack carries its own per-state art and audio. */
export type CrewAvatarOverride =
  | { kind: 'ghost'; traits?: KiroGhostTraits; motions?: AvatarMotions; sounds?: AvatarSounds }
  | {
      kind: 'image'
      v?: number
      file?: string
      pendingData?: string
      promote?: boolean
      token?: string
    }
  | { kind: 'pack'; id: string }

const TILE_RE = /^#[0-9a-f]{6}$/

/** Cap on a pack id, mirroring `MAX_PACK_ID_LEN` in `appearance_packs.py`. */
const MAX_PACK_ID_LEN = 64

/**
 * Letters, digits, dash and underscore — the backend's own rule (`safe_pack_id`:
 * `c.isalnum() or c in "-_"`), and `isalnum()` is UNICODE-aware. `\p{L}` and
 * `\p{N}` are what it accepts, so `auróra` and `아우로라` are legal ids that the
 * importer really does install. An ASCII-only class read such a record as "no
 * override", and the first unrelated save then wrote `{}` over it — the exact
 * data loss this component's pack tier exists to stop.
 */
const PACK_ID_RE = /^[\p{L}\p{N}_-]+$/u

/**
 * Interpret a crew record's `avatar` field as an appearance-pack override.
 * Returns the pack id, or `null` when the field is absent, junk, another tier,
 * or names an id the library could not hold. Total for the same reason as
 * `ghostTraitsFrom`: roster rows carry the field untyped.
 *
 * A rejected id resolves to "no override", which renders the name-derived
 * ghost — the same thing an absent pack already shows.
 */
export function packAvatarFrom(avatar: unknown): { id: string } | null {
  if (!avatar || typeof avatar !== 'object') return null
  const a = avatar as Record<string, unknown>
  if (a.kind !== 'pack' || typeof a.id !== 'string') return null
  // Trimmed, because the backend trims before it stores or resolves: the pack it
  // holds for `" aurora "` is `aurora`, so that is the id the slot URL must name.
  const id = a.id.trim()
  // Code POINTS, not UTF-16 units, so an astral character counts as the one
  // character the backend counts it as.
  if (!id || [...id].length > MAX_PACK_ID_LEN || !PACK_ID_RE.test(id)) return null
  return { id }
}

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
 * Interpret a crew record's `avatar` field. Returns the PINNED traits, or
 * `null` for "no pinned face" (absent, `{}`, junk, or a ghost override that
 * carries only reactions — the backend collapses junk to `{}`, but this stays
 * total because MembersPage rows carry the field untyped). Null is the answer
 * a caller needs: it distinguishes "this crew chose a face" from "use the
 * name-derived one", which is what the editor's dirty check and this
 * component's cache both key on. Resolving null to the seeded face is the
 * RENDERER's job, one function down.
 *
 * Unknown trait options are kept verbatim: `compose` resolves them to "absent"
 * (`EYES[k] ?? ''`), which is the same forgiveness the backend applies, so an
 * old client renders a face saved by a newer vocabulary without crashing.
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
 * Does this record wear a customized face? The SAME coercion the renderer
 * applies, so "default" here means exactly "the name-derived ghost renders".
 * Callers must not test the raw field: the backend stores `{}` for no
 * override, and `{}` is truthy.
 */
export function hasAvatarOverride(avatar: unknown): boolean {
  return (
    ghostTraitsFrom(avatar) !== null ||
    imageAvatarFrom(avatar) !== null ||
    packAvatarFrom(avatar) !== null
  )
}

/**
 * The stored `avatar` value when NO reader here claims it — a tier a newer
 * client wrote, or a pack id this build cannot parse — and `null` otherwise
 * (including for `{}`, which already means "no override").
 *
 * This is what lets a save PRESERVE a record it does not understand. The crew
 * editor's payload is `draft ?? {}`, so an unrecognised record is erased by the
 * first unrelated edit — and that wipe is a property of the enumeration, not of
 * any one tier: adding a third reader would only leave the fourth tier to be
 * broken the same way. Carrying the raw value through retires the whole class.
 */
export function unclaimedAvatarFrom(avatar: unknown): Record<string, unknown> | null {
  if (!avatar || typeof avatar !== 'object' || Array.isArray(avatar)) return null
  const raw = avatar as Record<string, unknown>
  if (!Object.keys(raw).length) return null
  // The KIND is the discriminator, not which keys happen to parse. A record
  // that NAMES a tier no reader above claimed describes a face this build
  // cannot reproduce, and that is true whether the tier is one a newer client
  // invented (`hologram`) or one of ours carrying a payload we reject (a pack
  // id past the id rule). It has to be decided here rather than left to the
  // three readers: a reader asked only whether it can see a reaction map would
  // answer "mine" about `{kind:'hologram', sounds:{done:'chime'}}` on the
  // strength of the chime, the record would read as understood, and the
  // hologram would be dropped by the next unrelated save — the very wipe the
  // passthrough exists to retire, surviving for every unknown tier that
  // happens to carry a reaction. The reaction readers are ghost-gated now, so
  // they cannot make that mistake either, and the kind check keeps holding the
  // class shut for a tier that carries no reaction at all.
  //
  // `ghost` is the one named kind that does not count as a tier claim: with no
  // pinned traits it IS the name-derived default every reader agrees on, so
  // `{kind:'ghost', sounds:{…}}` is a fully-understood reactions-on-default
  // record and must keep falling through to the readers below.
  const kind = typeof raw.kind === 'string' ? raw.kind.trim() : ''
  if (kind && kind !== 'ghost' && !hasAvatarOverride(avatar)) return raw
  if (hasAvatarOverride(avatar) || motionsFrom(avatar) || soundsFrom(avatar)) return null
  return raw
}

/**
 * Generated data URIs for NAME-SEEDED avatars only, keyed by seed + working
 * intensity + reaction (NUL-joined so the parts cannot collide with a seed
 * containing the tier word). Module-level rather than per-component so a crew's
 * avatar is generated once per session even though it is rendered in both the
 * roster card and the editor panel; a crew has at most five entries (still,
 * subtle, full, and one reaction per reacting state). Pinned-trait faces are
 * deliberately NOT cached here: the builder generates a fresh trait combination
 * on every picker click, so a trait-keyed entry would accumulate one encoded
 * SVG per click for the life of the tab. The component's own useMemo covers the
 * pinned path.
 *
 * The reaction is safe to key on for the seeded path because it comes off the
 * record rather than off a picker: a crew's own motion names are fixed, so the
 * entry count is bounded by the states, not by clicks.
 */
const CACHE = new Map<string, string>()

/**
 * The name-derived traits per seed. Deriving them runs a full DiceBear draw,
 * and the expression path needs them on every render of an unpinned crew that
 * is reacting, so the draw is done once per seed. A COPY is handed out: the
 * builder spreads what it gets, and one caller mutating a shared record would
 * silently change every other crew card drawn from the same seed.
 */
const SEEDED_TRAITS = new Map<string, KiroGhostTraits>()

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
  /**
   * Which reaction to render. `idle` (the default) is the resting face.
   * `working` animates through the eye-bound working variant; `done` and
   * `error` play the ghost's built-in motion for that state.
   */
  state?: AvatarFaceState
  /** Animate the ghost as "at work". `subtle` for dense lists, `full` for a
   *  single-avatar surface. Identity is untouched — the working variant only
   *  moves what the face drew — so omitting it is a lossless still frame.
   *
   *  Kept alongside `state` for back-compat: set on its own it MEANS
   *  `state: 'working'`, and alongside `state` it only chooses the intensity
   *  of the working animation. */
  working?: WorkingIntensity
  /** Fired when served art fails to load — an uploaded picture, or a pack that
   *  cannot be read or draws nothing — before the seeded-ghost fallback renders.
   *  Surfaces the failure where a bare fallback would read as "saved fine": the
   *  editor shows an inline warning through it, naming which tier failed. */
  onImageError?: () => void
  className?: string
}

export default function CrewAvatar({
  seed,
  avatar,
  size = 40,
  state,
  working,
  onImageError,
  className = '',
}: CrewAvatarProps) {
  const traits = useMemo(() => ghostTraitsFrom(avatar), [avatar])
  const image = useMemo(() => imageAvatarFrom(avatar), [avatar])
  const pack = useMemo(() => packAvatarFrom(avatar), [avatar])
  // An explicit `state` wins; a bare `working` is the older spelling of it.
  const shownState: AvatarFaceState = state ?? (working ? 'working' : 'idle')
  // The built-in pack IS the name-derived ghost, and its art ships in this
  // bundle rather than being served — so it takes the ghost path below and
  // fetches nothing. Every other pack is art this build cannot compose, drawn by
  // `PackAvatar`, which resolves the same fallback chain the slot route does, so
  // a pack that draws only `idle` still answers every state.
  const packId = pack && pack.id !== BUILTIN_PACK_ID ? pack.id : null
  // A reaction is the ghost's: `motionsFrom` is ghost-gated, and the tier guard
  // repeats that here so the DEFAULT motion cannot reach a served tier either —
  // the ghost drawn as a broken picture's fallback is a still face, not a crew
  // reporting a turn it never ran. Sounds are the other half of the reaction
  // layer and are deliberately NOT this component's business: they belong to
  // the state hook.
  const motions = useMemo(
    () => (image || packId ? null : motionsFrom(avatar)),
    [avatar, image, packId],
  )
  // Memo-stable so it cannot churn the src memo below.
  const reaction = useMemo(
    () => (image || packId ? null : motionFor(motions, shownState)),
    [motions, shownState, image, packId],
  )
  const intensity = shownState === 'working' ? (working ?? 'subtle') : undefined
  // An uploaded picture that fails to load (file deleted out-of-band, stale
  // record) falls back to the name-derived ghost rather than the browser's
  // broken-image glyph. Keyed by the src so a REPLACED picture gets a fresh
  // chance instead of inheriting the previous file's failure.
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  const src = useMemo(() => {
    // Pinned traits render fresh (see the CACHE comment); useMemo already
    // dedupes re-renders of one mounted instance. Intensity and reaction are
    // render parameters of the same compose() path, so a customized face
    // animates and reacts exactly like a seeded one.
    if (traits) return ghostDataUri(traits, intensity, reaction)
    const key = [seed, intensity ?? '', reaction ? `${reaction.state}:${reaction.name}` : ''].join(
      '\u0000',
    )
    const hit = CACHE.get(key)
    if (hit) return hit
    // A reaction resolves the seeded traits and composes them directly: the
    // DiceBear style takes the working intensity as an option, and giving it a
    // motion option too would make a render parameter look like a trait.
    //
    // The tile color is part of the style rather than a `backgroundColor` list,
    // so that it is drawn from the same seeded stream as every other trait.
    const uri = reaction
      ? ghostDataUri(seededTraits(seed), intensity, reaction)
      : createAvatar(STYLE, {
          seed,
          radius: GHOST_RADIUS_PCT,
          working: intensity,
        }).toDataUri()
    CACHE.set(key, uri)
    return uri
  }, [seed, traits, reaction, intensity])

  // The editor draft's not-yet-uploaded picture previews directly; a saved
  // one is served by the authenticated API (same-origin cookie auth), with
  // the upload stamp as the cache-buster so a replaced face shows up without
  // waiting out the browser cache.
  const imageSrc = image
    ? (image.pendingData ??
      `/api/agents/${encodeURIComponent(seed)}/avatar${image.v ? `?v=${image.v}` : ''}`)
    : null
  const ghost = (
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

  // The pack tier. NOT latched the way the picture tier is: `PackAvatar` owns
  // the read, and a read that failed (a gateway blip) is refetched on the next
  // focus, reconnect or Library invalidation — but only while the component
  // that asked is still mounted to receive the answer. So the ghost goes IN as
  // the fallback and `PackAvatar` draws it in place; unmounting it here would
  // make every blip a ghost until the record names a different pack. A renderer
  // failure is reset inside `PackAvatar` when the id or the art changes, which
  // is the same fresh chance the picture tier's src key gives.
  if (packId) {
    return (
      <PackAvatar
        id={packId}
        state={shownState}
        size={size}
        className={className}
        onError={onImageError}
        fallback={ghost}
      />
    )
  }

  // A picture is art this component did not compose either, fetched from the
  // authenticated API and falling back to the seeded ghost when it does not
  // load. The two served tiers are exclusive, and a record carrying neither
  // leaves both null and takes the ghost path.
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

  return ghost
}

/** The name-derived traits for a seed — the builder's pre-fill, its "reset to
 *  default" preview, and the base an expression overlays when the crew pinned
 *  no face of its own. Same draw the roster made, read back out. */
export function seededTraits(seed: string): KiroGhostTraits {
  const hit = SEEDED_TRAITS.get(seed)
  if (hit) return { ...hit }
  const extra = createAvatar(STYLE, { seed, radius: GHOST_RADIUS_PCT }).toJson().extra
  const drawn = ghostTraitsFrom({ kind: 'ghost', traits: extra }) as KiroGhostTraits
  SEEDED_TRAITS.set(seed, drawn)
  return { ...drawn }
}
