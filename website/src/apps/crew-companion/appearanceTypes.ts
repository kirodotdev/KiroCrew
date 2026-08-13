/**
 * CrewCompanion - Appearance Pack type definitions
 *
 * Defines the data model for appearance packs, including pack metadata,
 * animation mappings, manifest structure, and related utility types.
 */

// ── Animation & Pack Types ─────────────────────────────────────────────────

/** Supported animation formats */
export type AnimationFormat = 'svg' | 'lottie' | 'sprite'

/** Whether a pack ships with the app or was created by the user */
export type PackType = 'built-in' | 'custom'

// ── Required States & Moods ────────────────────────────────────────────────

/** The only state every appearance pack must provide. Everything else falls
 *  back through the resolver (see AnimationResolver), so one idle image is a
 *  complete, valid avatar. */
export const REQUIRED_STATES = ['idle'] as const

/**
 * The pack's optional slots, and the ONE definition of what categories exist. The
 * editor and the pack detail view both read from here, so "what you can draw" and
 * "what a pack shows" can't drift apart (they had: the editor asked for Busy/Error/
 * Offline while the app also wanted done and loading art it never requested).
 *
 *   STATUS  — reactions driven by what the agent is doing. These are the three
 *             signals the app actually produces: a turn finished, a turn failed, a
 *             turn is running.
 *   RANDOM  — spontaneous behaviour the pet plays on its own, plus any clips the
 *             author names themselves.
 *
 * `thinking` / `working` are kept as legacy aliases of `loading` so packs authored
 * before this split keep working; they're not offered as slots any more.
 */
export const STATUS_STATES = ['done', 'error'] as const

/**
 * BREATHING — one drawing per phase of the guided exercise. All optional: a pack
 * that supplies none keeps the default behaviour, which is the idle body driven by
 * the app's own scale and opacity motion. Supplying only some is fine too; each
 * missing phase falls back to idle.
 */
export const BREATHING_STATES = ['inhale', 'hold', 'exhale'] as const

export const RANDOM_STATES = ['walking'] as const

/**
 * `loading` joined the legacy list rather than being deleted: packs authored while
 * it was offered still carry that art, and it is still read as an alias for
 * thinking/working. It is no longer OFFERED because after the chat and planner were
 * removed, voice dictation is the only thing that enters those states — so an
 * author could not see what they were drawing for.
 */
export const LEGACY_STATES = [
  'thinking', 'working', 'offline', 'peeking', 'peekThinking', 'loading',
] as const

export const OPTIONAL_STATES = [
  ...STATUS_STATES, ...BREATHING_STATES, ...RANDOM_STATES, ...LEGACY_STATES,
] as const

/** Labels for pack slots, shared by the editor and the pack detail view. */
/*
 * NOTE: the slot display labels ("Idle", "Breathe in", …) are deliberately NOT here.
 *
 * They are user-facing text, so they belong in the i18n catalogue rather than as
 * English string literals — and nothing renders them until the gallery UI is ported,
 * which is the surface that shows them. Adding them as literals now would ship
 * untranslated strings that no screen displays.
 */

/** All PetMood values that an appearance pack may optionally provide animations for */
export const ALL_MOODS = ['happy', 'sleepy', 'curious', 'busy', 'scared'] as const

// ── Pack Metadata ──────────────────────────────────────────────────────────

/** Metadata describing an appearance pack */
export interface PackMeta {
  /** Unique identifier for the pack */
  id: string
  /** Human-readable display name */
  name: string
  /** Pack author / creator */
  author: string
  /** Description of what this character is (e.g. "a cute orange cat", "a pixel robot") — used as personality prompt context */
  description: string
  /** Whether this is a built-in or user-created pack */
  type: PackType
  /** Primary animation format (derived from the idle animation) */
  format: AnimationFormat
  /** Relative path to the thumbnail image within the pack directory */
  thumbnail: string
}

// ── Animation Mappings ─────────────────────────────────────────────────────

/**
 * Maps each required PetState to its animation file path (relative to pack root).
 * All six states must be present for a valid appearance pack.
 */
export interface StateAnimationMap {
  idle: string
  // Status
  done?: string
  error?: string
  // Breathing — one per phase of the guided exercise, each falling back to idle
  inhale?: string
  hold?: string
  exhale?: string
  // Random
  walking?: string
  // Legacy — still honoured for packs made before the status/random split
  loading?: string
  thinking?: string
  working?: string
  offline?: string
  peeking?: string
  peekThinking?: string
}

/**
 * Maps optional PetMood values to animation file paths (relative to pack root).
 * When a mood animation is present, it takes priority over the state animation.
 */
export interface MoodAnimationMap {
  happy?: string
  sleepy?: string
  curious?: string
  busy?: string
  scared?: string
}

// ── Manifest & Resolved Pack ───────────────────────────────────────────────

/** Complete appearance pack manifest describing metadata and animation mappings */
/** Sprite sheet metadata — only present when format is 'sprite' */
export interface SpriteConfig {
  frameWidth: number
  frameHeight: number
  fps: number
  flipX?: boolean
  offsetY?: number
  source?: string
  /** state/mood key → row index (for re-editing) */
  rowAssignments?: Record<string, number>
}

export interface PackManifest {
  /** Pack metadata */
  meta: PackMeta
  /** Required state-to-animation mappings */
  states: StateAnimationMap
  /** Optional mood-to-animation mappings */
  moods: MoodAnimationMap
  /** Open-ended "random" clips (name → filename) the pet plays spontaneously.
   *  Users can add anything here (a wave, a spin, a snack…). */
  random?: Record<string, string>
  /** Sprite config — present when meta.format is 'sprite' */
  sprite?: SpriteConfig
}

/** A fully resolved appearance pack with its absolute base path on disk */
export interface ResolvedPack {
  /** The parsed pack manifest */
  manifest: PackManifest
  /** Absolute path to the pack root directory */
  basePath: string
}

// ── Animation Source ───────────────────────────────────────────────────────

/** Resolved animation source ready for rendering */
export interface AnimationSource {
  /** Data URI (for SVG) or JSON string (for Lottie) */
  uri: string
  /** The animation format */
  format: AnimationFormat
}

// ── Result Type ────────────────────────────────────────────────────────────

/**
 * A discriminated union representing either a successful value or an error.
 * Used throughout the appearance system for structured error handling.
 */
export type Result<T, E = string> =
  | { ok: true; value: T }
  | { ok: false; error: E }

// ── Manifest Serialization / Parsing ───────────────────────────────────────

/**
 * Serializes a PackManifest to a pretty-printed JSON string (2-space indent).
 */
export function serializeManifest(manifest: PackManifest): string {
  return JSON.stringify(manifest, null, 2)
}

/**
 * Parses a JSON string into a PackManifest with basic structural validation.
 *
 * Checks that `meta` exists with required fields, `states` exists with all
 * six required keys, and `moods` exists (can be empty object).
 * Detailed validation (file existence, etc.) is handled elsewhere.
 */
export function parseManifest(json: string): Result<PackManifest, string> {
  let parsed: unknown
  try {
    parsed = JSON.parse(json)
  } catch {
    return { ok: false, error: 'Invalid JSON' }
  }

  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { ok: false, error: 'Manifest must be a JSON object' }
  }

  const obj = parsed as Record<string, unknown>

  // Validate meta
  if (typeof obj.meta !== 'object' || obj.meta === null || Array.isArray(obj.meta)) {
    return { ok: false, error: 'Missing or invalid "meta" field' }
  }
  const meta = obj.meta as Record<string, unknown>
  const requiredMetaFields = ['id', 'name', 'author', 'type', 'format', 'thumbnail']
  for (const field of requiredMetaFields) {
    if (typeof meta[field] !== 'string') {
      return { ok: false, error: `Missing or invalid meta field: "${field}"` }
    }
  }

  // Validate states
  if (typeof obj.states !== 'object' || obj.states === null || Array.isArray(obj.states)) {
    return { ok: false, error: 'Missing or invalid "states" field' }
  }
  const states = obj.states as Record<string, unknown>
  for (const state of REQUIRED_STATES) {
    if (typeof states[state] !== 'string') {
      return { ok: false, error: `Missing or invalid state mapping: "${state}"` }
    }
  }

  // Validate moods
  if (typeof obj.moods !== 'object' || obj.moods === null || Array.isArray(obj.moods)) {
    return { ok: false, error: 'Missing or invalid "moods" field' }
  }

  // Explicitly extract sprite config if present.
  //
  // And DROP it when it is present but not an object: the cast below carries every
  // field of the parsed JSON through, so a `"sprite": "nope"` survived as a string
  // typed `SpriteConfig` and reached the sprite renderer, which then read
  // frameWidth/frameHeight/fps off a string and got undefined for each.
  const sprite = (typeof obj.sprite === 'object' && obj.sprite !== null)
    ? obj.sprite as SpriteConfig : undefined

  const manifest = parsed as PackManifest
  if (sprite) manifest.sprite = sprite
  else delete manifest.sprite
  return { ok: true, value: manifest }
}

// ── Manifest Structure Validation ──────────────────────────────────────────




/**
 * Validates the structure of an unknown value as a PackManifest.
 *
 * Takes any parsed JSON value and returns an array of human-readable error
 * strings describing every structural problem found. An empty array means
 * the value is structurally valid.
 *
 * Checks performed:
 * - `meta` exists and is an object with all required string fields
 * - `meta.type` is 'built-in' or 'custom'
 * - `meta.format` is 'svg' or 'lottie'
 * - `states` exists and contains all six required PetState keys with non-empty string values
 * - `moods` exists and is an object
 */

// ── Format Validation ──────────────────────────────────────────────────────

/**
 * Checks whether the given string content looks like valid SVG.
 * Performs a simple case-insensitive check for the `<svg` opening tag.
 */
export function isValidSvg(content: string): boolean {
  return content.toLowerCase().includes('<svg')
}

/**
 * Checks whether the given string content is valid Lottie JSON.
 * Returns true if the content parses as JSON and the resulting object
 * contains the required Lottie fields: `v`, `fr`, `ip`, `op`, and `layers`.
 */
export function isValidLottie(content: string): boolean {
  let parsed: unknown
  try {
    parsed = JSON.parse(content)
  } catch {
    return false
  }

  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return false
  }

  const obj = parsed as Record<string, unknown>
  return 'v' in obj && 'fr' in obj && 'ip' in obj && 'op' in obj && 'layers' in obj
}
