/**
 * Per-state reaction overrides for a crew avatar.
 *
 * A crew's avatar has one IDENTITY — the name-derived face, or the traits
 * pinned in the builder — and, on the ghost tier, a small REACTION layer on top
 * of it: a built-in motion when a turn finishes and when it ends badly, plus a
 * sound for either of those moments and for the work itself.
 *
 * Identity is never part of a reaction. A motion may move the ghost and change
 * its eyes for the moment it plays; it cannot change hat, tile or mouth, and a
 * crew that did would read as a different crew, which is the opposite of what
 * an avatar is for. The vocabulary of motions lives in `kiroGhostAvatar.ts`
 * beside the art that implements it.
 *
 * Reactions belong to the tier that can play them, so both readers here are
 * GHOST-ONLY. A picture is static and silent — there is no face to move and no
 * cue it could carry that the ghost's own vocabulary would fit. A pack brings
 * its own per-state art and its own audio files, so a preset on the crew record
 * would be a second, competing answer to the same question.
 *
 * The coercions are TOTAL, for the same reason `ghostTraitsFrom` is: these
 * values come off a config record that older and newer clients both write, and
 * a roster row carries the field untyped. An unknown MOTION name is kept
 * verbatim — `motionFrom` resolves it to the still frame, so a reaction saved
 * by a newer vocabulary renders as no reaction instead of crashing, and is
 * never replaced by a substitute the crew's author did not choose. What IS
 * rejected is a value of the wrong shape, which collapses to "no override"
 * rather than reaching the renderer.
 */
import { SOUND_PRESETS, type SoundPreset } from '../hooks/useNotificationSound'
import { MOTION_STATES, type MotionRender, type MotionState } from './kiroGhostAvatar'

/** The states a crew avatar reacts to. `idle` is the absence of all three. */
export const AVATAR_STATES = ['working', 'done', 'error'] as const
export type AvatarState = (typeof AVATAR_STATES)[number]
/** What a call site renders: one reacting state, or the resting face. */
export type AvatarFaceState = 'idle' | AvatarState

/** One built-in motion name per state that has one. */
export type AvatarMotions = Partial<Record<MotionState, string>>
export type AvatarSounds = Partial<Record<AvatarState, SoundPreset>>

/**
 * What a ghost plays for a state the record says nothing about.
 *
 * A reaction is the point of the layer, so the default is a reaction rather
 * than stillness: a crew nobody has customized still tells you its turn
 * finished. Choosing `none` in the builder is how a crew opts out, and that is
 * a stored value distinct from an absent one.
 */
export const MOTION_DEFAULTS: Record<MotionState, string> = {
  done: 'bounce',
  error: 'shake',
}

/** `'none'` is a stored value, not an absence: it says "deliberately silent". */
const VALID_SOUNDS: ReadonlySet<string> = new Set<string>(['none', ...SOUND_PRESETS])

/** Is this record the ghost tier? Reactions are the ghost's alone, and `kind`
 *  is the discriminator — a record that pins no traits is still a ghost. */
function isGhost(avatar: unknown): avatar is Record<string, unknown> {
  return !!avatar && typeof avatar === 'object' && (avatar as Record<string, unknown>).kind === 'ghost'
}

/**
 * Read the `motions` map off a crew record's `avatar` field. Returns null for
 * "nothing configured", so a caller can test the whole layer with one check —
 * and for every tier but the ghost, which has no motion to play.
 */
export function motionsFrom(avatar: unknown): AvatarMotions | null {
  if (!isGhost(avatar)) return null
  const raw = avatar.motions
  if (!raw || typeof raw !== 'object') return null
  const byState = raw as Record<string, unknown>
  const out: AvatarMotions = {}
  for (const state of MOTION_STATES) {
    const name = byState[state]
    if (typeof name === 'string' && name) out[state] = name
  }
  return Object.keys(out).length ? out : null
}

/**
 * Read the `sounds` map off a crew record's `avatar` field. An unrecognised
 * preset is dropped rather than passed through: unlike a motion, a preset this
 * client cannot synthesize has no forgiving rendering — it would be silence
 * with no way to tell that from a deliberate one.
 */
export function soundsFrom(avatar: unknown): AvatarSounds | null {
  if (!isGhost(avatar)) return null
  const raw = avatar.sounds
  if (!raw || typeof raw !== 'object') return null
  const byState = raw as Record<string, unknown>
  const out: AvatarSounds = {}
  for (const state of AVATAR_STATES) {
    const preset = byState[state]
    if (typeof preset === 'string' && VALID_SOUNDS.has(preset)) out[state] = preset as SoundPreset
  }
  return Object.keys(out).length ? out : null
}

/**
 * Does this record still name a preset sound on a tier that no longer plays one?
 *
 * The inverse of `soundsFrom`'s ghost gate, and it exists for the ONE moment the
 * gate is not self-explanatory: a crew that was saved with a chime while wearing
 * a picture or a pack has gone quiet without its owner touching anything. The
 * builder says so in a line rather than letting the silence be discovered.
 *
 * Deliberately not "is there a `sounds` key": a junk value was never audible, so
 * reporting it as a lost sound would be a false claim. The presets are re-read
 * here (rather than delegating to `soundsFrom`) precisely BECAUSE that reader is
 * ghost-gated — this question is about the tiers it refuses.
 */
export type RetiredCue = {
  /** The tier the cue was saved ON, so the notice renders only on that tier's
   *  pane. Without it a saved-picture crew browsing the Library pane read
   *  "this pack plays its own sounds instead" with no pack worn -- a true
   *  sentence about the wrong tier, which is a false reason. */
  kind: 'image' | 'pack'
  states: AvatarState[]
}

export function retiredCueFrom(avatar: unknown): RetiredCue | null {
  if (!avatar || typeof avatar !== 'object') return null
  const record = avatar as Record<string, unknown>
  const kind = record.kind
  if (kind !== 'image' && kind !== 'pack') return null
  const raw = record.sounds
  if (!raw || typeof raw !== 'object') return null
  const byState = raw as Record<string, unknown>
  const states = AVATAR_STATES.filter(state => {
    const preset = byState[state]
    // `'none'` IS the stored silence, so a record carrying only that lost
    // nothing and gets no notice.
    return typeof preset === 'string' && preset !== 'none' && VALID_SOUNDS.has(preset)
  })
  return states.length ? { kind, states } : null
}

/**
 * A stored record's keys that THIS build no longer reads, kept beside the draft so
 * an unrelated save cannot be what deletes them.
 *
 * The retirement is a rendering decision, not a licence to erase: the backend
 * round-trips these keys for one more release, and the crew editor rewrites the
 * WHOLE `avatar` record on any save -- so a user who edits a crew's model would
 * otherwise silently lose a preset cue they saved on the previous release, having
 * never opened the avatar builder or seen the notice that explains it. The keys
 * ride back out untouched and inert; the backend PR that drops the fields is what
 * finally removes them.
 *
 * `kind` is the tier the keys were read FROM, so the caller can re-attach them
 * only to a record still on that tier. Switching tier is a deliberate act by the
 * user and takes the old tier's dead reaction keys with it.
 */
export type RetiredCarry = { kind: 'ghost' | 'image' | 'pack'; keys: Record<string, unknown> }

/** Per tier, the keys no reader in this build claims. */
const RETIRED_KEYS: Record<RetiredCarry['kind'], readonly string[]> = {
  // `motions` and `sounds` ARE read on a ghost, so only the retired expression
  // layer needs carrying; the live two travel on the draft like any edit.
  ghost: ['expressions'],
  // Both reaction readers are ghost-gated, so on these tiers every reaction key
  // is inert -- and every one of them is a value a previous release could write.
  image: ['expressions', 'motions', 'sounds'],
  pack: ['expressions', 'motions', 'sounds'],
}

export function retiredCarryFrom(avatar: unknown): RetiredCarry | null {
  if (!avatar || typeof avatar !== 'object') return null
  const record = avatar as Record<string, unknown>
  const kind = record.kind
  if (kind !== 'ghost' && kind !== 'image' && kind !== 'pack') return null
  const keys: Record<string, unknown> = {}
  for (const key of RETIRED_KEYS[kind]) {
    const value = record[key]
    // Object-shaped only: a junk scalar was never a reaction map, and handing one
    // back would re-assert something the validator would strip anyway.
    if (value && typeof value === 'object' && !Array.isArray(value)) keys[key] = value
  }
  return Object.keys(keys).length ? { kind, keys } : null
}

const isMotionState = (state: AvatarFaceState | undefined): state is MotionState =>
  state === 'done' || state === 'error'

/**
 * The reaction a rendered state plays, ready to hand to `compose`.
 *
 * Null for `idle` and for `working`: those are not transitions, and the working
 * animation is the ghost's own. A state with no stored choice falls back to
 * `MOTION_DEFAULTS`, so the reaction layer is on by default.
 */
export function motionFor(
  motions: AvatarMotions | null | undefined,
  state: AvatarFaceState | undefined,
): MotionRender | null {
  if (!isMotionState(state)) return null
  return { state, name: motions?.[state] ?? MOTION_DEFAULTS[state] }
}
