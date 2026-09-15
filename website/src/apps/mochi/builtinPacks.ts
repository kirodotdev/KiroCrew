/**
 * The built-in appearance packs.
 *
 * **Why built-ins are registered in the renderer at all.** Upstream's
 * `AppearanceRegistry.registerDefaultBuiltInPack()` put the cat into an
 * in-memory map at startup, so the gallery always had at least one entry. The
 * Python port only scans the data directory for USER packs, so a fresh install
 * would show an empty Avatars window. The art also lives in this bundle (Vite
 * `?raw` imports, not files the Python package ships), so the backend has
 * nothing to serve — registering here is the adaptation.
 *
 * **One identity key.** `activeAppearance` (a pack id) is the whole answer: it
 * drives the art, the persona, and the default pet name together. The original
 * had a second key, `pet.character`, and migrated it away for exactly that
 * reason; this port briefly re-created the split as `avatar`, which let the pet
 * render an imported pack while its prompt described the built-in cat. The
 * built-in packs below ARE the two characters — there is nothing else to select.
 */

import type { PackManifest } from './src/shared/appearanceTypes'

import idleSvg from './assets/animations/mochi_idle.svg?raw'
import walkingSvg from './assets/animations/mochi_walking.svg?raw'
import thinkingSvg from './assets/animations/mochi_thinking.svg?raw'
import workingSvg from './assets/animations/mochi_working.svg?raw'
import errorSvg from './assets/animations/mochi_error.svg?raw'
import sleepingSvg from './assets/animations/mochi_sleeping.svg?raw'
import peekSvg from './assets/animations/mochi_peek.svg?raw'
import peekThinkingSvg from './assets/animations/mochi_peek_thinking.svg?raw'
import doneSvg from './assets/animations/mochi_done.svg?raw'

import ghostIdle from './assets/animations/kiro_idle_mid.json?raw'
import ghostIdleBlink from './assets/animations/kiro_idle_mid_blink.json?raw'
import ghostStaticBlink from './assets/animations/kiro_static_blink.json?raw'
import ghostFlying from './assets/animations/kiro_flying.json?raw'

export interface InlineAnimation {
  content: string
  /** `sprite` is reachable for imported packs (petdex sheets are webp). */
  format: 'lottie' | 'svg' | 'sprite'
}

export interface PackDetail extends PackManifest {
  animations: Record<string, InlineAnimation>
}

export const BUILTIN_MOCHI_ID = 'default-mochi'
export const BUILTIN_GHOST_ID = 'kiro-ghost'

interface BuiltinPack {
  meta: PackManifest['meta']
  /** Slot -> animation CONTENT (not a filename: the art is compiled in). */
  states: Record<string, string>
  moods: Record<string, string>
  format: 'svg' | 'lottie'
  /** Extension used for the synthetic manifest filenames. */
  ext: 'svg' | 'json'
  /** The art faces left; every surface mirrors it. See PackManifest.flipX. */
  flipX?: boolean
}

const MOCHI_PACK: BuiltinPack = {
  format: 'svg',
  ext: 'svg',
  meta: {
    id: BUILTIN_MOCHI_ID,
    name: 'Mochi Cat',
    author: 'Mochi',
    description:
      'A cute, round orange cat with big expressive eyes. Playful and curious, ' +
      'loves to nap and chase things.',
    type: 'built-in',
    format: 'svg',
    thumbnail: 'mochi_idle.svg',
  },
  states: {
    idle: idleSvg,
    walking: walkingSvg,
    thinking: thinkingSvg,
    working: workingSvg,
    error: errorSvg,
    offline: sleepingSvg,
    peeking: peekSvg,
    peekThinking: peekThinkingSvg,
  },
  moods: {
    happy: doneSvg,
    sleepy: sleepingSvg,
    curious: thinkingSvg,
    busy: workingSvg,
    scared: errorSvg,
  },
}

/**
 * The Kiro ghost, built from the four delivered idle/fly Lotties.
 *
 * There are four clips for six states, so several states share one — accepted
 * deliberately: a ghost that reuses a float for "working" reads fine, whereas
 * leaving the avatar unregistered (its previous state — the files sat in the
 * bundle with zero importers) meant it could not be chosen at all.
 *
 * `peeking` / `peekThinking` are OMITTED rather than filled with a float. They
 * are optional, and the resolver falls back to `idle` / `thinking` for them; a
 * peek pose is a specific half-off-screen drawing that none of these clips is.
 */
const GHOST_PACK: BuiltinPack = {
  format: 'lottie',
  ext: 'json',
  // The delivered clips are mirrored: the ghost's tail trails to the RIGHT, so it
  // reads as drifting right-to-left while every other pack (and the pet's own
  // walk logic) assumes art that faces right. Declared here rather than
  // compensated for at each render site, and rather than un-mirroring the art:
  // the eyes are parented into BODY's already-negated coordinate space, so
  // flipping the layer scales would move them to the wrong side of the face.
  flipX: true,
  meta: {
    id: BUILTIN_GHOST_ID,
    name: 'Kiro Ghost',
    author: 'Kiro',
    description:
      'A small friendly ghost that floats and blinks. Calm and attentive, ' +
      'it drifts along while it works.',
    type: 'built-in',
    format: 'lottie',
    thumbnail: 'idle.json',
  },
  states: {
    idle: ghostIdle,
    walking: ghostFlying,
    thinking: ghostIdleBlink,
    working: ghostFlying,
    error: ghostStaticBlink,
    offline: ghostStaticBlink,
  },
  moods: {
    happy: ghostIdleBlink,
    sleepy: ghostStaticBlink,
    curious: ghostIdle,
    busy: ghostFlying,
    scared: ghostStaticBlink,
  },
}

const BUILTIN_PACKS: Record<string, BuiltinPack> = {
  [BUILTIN_MOCHI_ID]: MOCHI_PACK,
  [BUILTIN_GHOST_ID]: GHOST_PACK,
}

/** Gallery order: the default avatar first. */
export const BUILTIN_PACK_IDS: readonly string[] = [BUILTIN_MOCHI_ID, BUILTIN_GHOST_ID]

export function isBuiltinPack(packId: string): boolean {
  return packId in BUILTIN_PACKS
}

export function builtinPackMeta(packId: string): PackManifest['meta'] | null {
  return BUILTIN_PACKS[packId]?.meta ?? null
}

export function builtinPackMetas(): PackManifest['meta'][] {
  return BUILTIN_PACK_IDS.map((id) => BUILTIN_PACKS[id].meta)
}

/**
 * The pack id that is live.
 *
 * One key, one lookup. A missing or empty value resolves to the default pack
 * rather than to "nothing": the backend normalises the stored value the same
 * way, and a pet that renders the default character beats one that renders
 * nothing while a config is being written.
 */
export function resolveActivePackId(
  config: { activeAppearance?: unknown } | null | undefined,
): string {
  const explicit = config?.activeAppearance
  return typeof explicit === 'string' && explicit !== '' ? explicit : BUILTIN_MOCHI_ID
}

/**
 * What the pet CALLS ITSELF per built-in pack, when no explicit name is set.
 *
 * MIRRORS ``soul_loader.BUILTIN_PET_NAMES`` — the backend already resolves this
 * for the agent prompt, and the two must agree or the pet answers to one name in
 * chat and another in its own title bar.
 *
 * Deliberately NOT `meta.name`: that names the character DESIGN in the picker
 * ("Kiro Ghost" / "Mochi Cat"), while this is how the pet refers to itself.
 */
const BUILTIN_PET_NAMES: Record<string, string> = {
  [BUILTIN_MOCHI_ID]: 'Mochi',
  [BUILTIN_GHOST_ID]: 'Kiro',
}

/** Last-resort name, matching ``soul_loader.DEFAULT_PET_NAME``. */
export const DEFAULT_PET_NAME = 'Mochi'

/**
 * The name to address the pet by.
 *
 * Precedence: the user's explicit `petName`, else the ACTIVE PACK's own name,
 * else 'Mochi'. Every renderer used to do `useState('Mochi')` +
 * `if (c?.petName) setPetName(c.petName)`, which silently collapses the middle
 * rung: `petName` defaults to `""` and means "use the avatar's own name" (see
 * settings.py), so a user on the ghost with no custom name was told "Ask Mochi
 * to watch a price…" while the pet itself introduced itself as Kiro.
 *
 * A user-imported pack has no name of its own here, so it falls through to the
 * default rather than borrowing its display title — the pack's `meta.name` is a
 * design label ("Boba the Cat"), not an address.
 */
export function resolvePetName(
  config: { petName?: unknown; activeAppearance?: unknown } | null | undefined,
): string {
  const explicit = config?.petName
  if (typeof explicit === 'string' && explicit.trim() !== '') return explicit.trim()
  return BUILTIN_PET_NAMES[resolveActivePackId(config)] ?? DEFAULT_PET_NAME
}

/**
 * Built-in pack detail, in the flattened shape the vendored renderer reads.
 *
 * The manifest's state/mood values are FILENAMES upstream, so synthetic names
 * are generated to keep the shape identical; every read site consumes
 * `animations[slot].content`.
 */
export function builtinPackDetail(packId: string): PackDetail | null {
  const pack = BUILTIN_PACKS[packId]
  if (!pack) return null
  const animations: Record<string, InlineAnimation> = {}
  for (const [slot, content] of Object.entries({ ...pack.states, ...pack.moods })) {
    animations[slot] = { content, format: pack.format }
  }
  return {
    meta: pack.meta,
    ...(pack.flipX === true ? { flipX: true } : {}),
    states: Object.fromEntries(
      Object.keys(pack.states).map((k) => [k, `${k}.${pack.ext}`]),
    ) as unknown as PackManifest['states'],
    moods: Object.fromEntries(
      Object.keys(pack.moods).map((k) => [k, `${k}.${pack.ext}`]),
    ) as unknown as PackManifest['moods'],
    animations,
  }
}
