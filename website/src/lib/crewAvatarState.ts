/**
 * Per-state expression and sound overrides for a crew avatar.
 *
 * A crew's avatar has one IDENTITY — the name-derived face, or the traits
 * pinned in the builder — and, optionally, a small reaction layer on top of
 * it: a different pair of eyes and mouth while the crew is working, when a
 * turn finishes, and when a turn ends badly, plus a sound for each of those
 * moments.
 *
 * Only `eyes` and `mouth` may vary per state. Every other axis is identity:
 * a crew that changes hat or tile colour when it starts working reads as a
 * different crew, which is the opposite of what an avatar is for.
 *
 * The coercions here are TOTAL, for the same reason `ghostTraitsFrom` is:
 * these values come off a config record that older and newer clients both
 * write, and a roster row carries the field untyped. Unknown option names are
 * kept verbatim rather than rejected — `compose` resolves an unknown key to
 * "absent", so a face saved by a newer vocabulary renders on an old client
 * instead of crashing. What IS rejected is a value of the wrong shape, which
 * collapses to "no override" rather than reaching the renderer.
 */
import { SOUND_PRESETS, type SoundPreset } from '../hooks/useNotificationSound'
import type { KiroGhostTraits } from './kiroGhostAvatar'

/** The states a crew avatar reacts to. `idle` is the absence of all three. */
export const AVATAR_STATES = ['working', 'done', 'error'] as const
export type AvatarState = (typeof AVATAR_STATES)[number]
/** What a call site renders: one reacting state, or the resting face. */
export type AvatarFaceState = 'idle' | AvatarState

/** The two axes a state may vary. An absent axis means "same as idle". */
export interface AvatarExpression {
  eyes?: string
  mouth?: string
}

export type AvatarExpressions = Partial<Record<AvatarState, AvatarExpression>>
export type AvatarSounds = Partial<Record<AvatarState, SoundPreset>>

/** `'none'` is a stored value, not an absence: it says "deliberately silent". */
const VALID_SOUNDS: ReadonlySet<string> = new Set<string>(['none', ...SOUND_PRESETS])

/**
 * One state's expression, or null when it varies nothing. An empty string is
 * the UI's spelling of "same as idle", so it is dropped here rather than
 * reaching `compose` as an unknown key.
 */
function expressionFrom(value: unknown): AvatarExpression | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  const out: AvatarExpression = {}
  if (typeof raw.eyes === 'string' && raw.eyes) out.eyes = raw.eyes
  if (typeof raw.mouth === 'string' && raw.mouth) out.mouth = raw.mouth
  return out.eyes || out.mouth ? out : null
}

/**
 * Read the `expressions` map off a crew record's `avatar` field. Returns null
 * for "nothing to overlay", so a caller can test the whole layer with one
 * check instead of three.
 */
export function expressionsFrom(avatar: unknown): AvatarExpressions | null {
  if (!avatar || typeof avatar !== 'object') return null
  const raw = (avatar as Record<string, unknown>).expressions
  if (!raw || typeof raw !== 'object') return null
  const byState = raw as Record<string, unknown>
  const out: AvatarExpressions = {}
  for (const state of AVATAR_STATES) {
    const expression = expressionFrom(byState[state])
    if (expression) out[state] = expression
  }
  return Object.keys(out).length ? out : null
}

/**
 * Read the `sounds` map off a crew record's `avatar` field. An unrecognised
 * preset is dropped rather than passed through: unlike a face trait, a preset
 * this client cannot synthesize has no forgiving rendering — it would be
 * silence with no way to tell that from a deliberate one.
 */
export function soundsFrom(avatar: unknown): AvatarSounds | null {
  if (!avatar || typeof avatar !== 'object') return null
  const raw = (avatar as Record<string, unknown>).sounds
  if (!raw || typeof raw !== 'object') return null
  const byState = raw as Record<string, unknown>
  const out: AvatarSounds = {}
  for (const state of AVATAR_STATES) {
    const preset = byState[state]
    if (typeof preset === 'string' && VALID_SOUNDS.has(preset)) out[state] = preset as SoundPreset
  }
  return Object.keys(out).length ? out : null
}

/** The expression for a rendered state — `idle` never has one. */
export function expressionFor(
  expressions: AvatarExpressions | null | undefined,
  state: AvatarFaceState | undefined,
): AvatarExpression | null {
  if (!expressions || !state || state === 'idle') return null
  return expressions[state] ?? null
}

/**
 * Overlay one state's eyes and mouth onto the identity traits. Returns the
 * base object itself when there is nothing to vary, so the pure-identity path
 * allocates nothing.
 */
export function applyExpression(
  base: KiroGhostTraits,
  expression: AvatarExpression | null | undefined,
): KiroGhostTraits {
  if (!expression || (!expression.eyes && !expression.mouth)) return base
  return {
    ...base,
    ...(expression.eyes ? { eyes: expression.eyes } : {}),
    ...(expression.mouth ? { mouth: expression.mouth } : {}),
  }
}
