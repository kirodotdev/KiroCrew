/**
 * Mochi - Appearance Pack type definitions
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

/** All PetState values that every appearance pack must provide animations for */
export const REQUIRED_STATES = ['idle', 'walking', 'thinking', 'working', 'error', 'offline'] as const
export const OPTIONAL_STATES = ['peeking', 'peekThinking'] as const

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
  walking: string
  thinking: string
  working: string
  error: string
  offline: string
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
  /** Sprite config — present when meta.format is 'sprite' */
  sprite?: SpriteConfig
  /**
   * The pack's art faces LEFT rather than right, so every surface must mirror it.
   *
   * A pack-level fact, not a sprite one: `SpriteConfig.flipX` could only speak for
   * sprite sheets, but a hand-drawn SVG or an exported Lottie can just as easily
   * come out mirrored (the built-in Kiro Ghost does). Consumers should read
   * `flipX ?? sprite?.flipX` so an existing sprite pack keeps working unchanged.
   *
   * This is the pack's BASELINE facing, which situational flips (walk direction,
   * peeking off the right edge) XOR against — see PetWidget's transform.
   */
  flipX?: boolean
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

  // Explicitly extract sprite config if present
  const sprite = (typeof obj.sprite === 'object' && obj.sprite !== null)
    ? obj.sprite as SpriteConfig : undefined

  const manifest = parsed as PackManifest
  if (sprite) manifest.sprite = sprite
  return { ok: true, value: manifest }
}

// ── Manifest Structure Validation ──────────────────────────────────────────

/** Required fields on the `meta` object */
const REQUIRED_META_FIELDS = ['id', 'name', 'author', 'type', 'format', 'thumbnail'] as const

/** Valid values for `meta.type` */
const VALID_PACK_TYPES: readonly string[] = ['built-in', 'custom']

/** Valid values for `meta.format` */
const VALID_FORMATS: readonly string[] = ['svg', 'lottie', 'sprite']

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
export function validateManifestStructure(manifest: unknown): string[] {
  const errors: string[] = []

  if (typeof manifest !== 'object' || manifest === null || Array.isArray(manifest)) {
    // Through the same `errors` sink as every other diagnostic below rather than a
    // second returned literal: one shape for one kind of message.
    errors.push('Manifest must be a non-null object')
    return errors
  }

  const obj = manifest as Record<string, unknown>

  // ── meta validation ────────────────────────────────────────────────────
  if (typeof obj.meta !== 'object' || obj.meta === null || Array.isArray(obj.meta)) {
    errors.push('Missing or invalid "meta" field')
  } else {
    const meta = obj.meta as Record<string, unknown>

    for (const field of REQUIRED_META_FIELDS) {
      if (typeof meta[field] !== 'string' || (meta[field] as string).length === 0) {
        errors.push(`Missing or invalid meta field: "${field}"`)
      }
    }

    // type enum check (only if it's a string — missing string already reported above)
    if (typeof meta.type === 'string' && !VALID_PACK_TYPES.includes(meta.type)) {
      errors.push(`Invalid meta.type: "${meta.type}" (expected "built-in" or "custom")`)
    }

    // format enum check
    if (typeof meta.format === 'string' && !VALID_FORMATS.includes(meta.format)) {
      errors.push(`Invalid meta.format: "${meta.format}" (expected "svg", "lottie", or "sprite")`)
    }
  }

  // ── states validation ──────────────────────────────────────────────────
  if (typeof obj.states !== 'object' || obj.states === null || Array.isArray(obj.states)) {
    errors.push('Missing or invalid "states" field')
  } else {
    const states = obj.states as Record<string, unknown>
    for (const state of REQUIRED_STATES) {
      if (typeof states[state] !== 'string' || (states[state] as string).length === 0) {
        errors.push(`Missing or invalid state: "${state}"`)
      }
    }
  }

  // ── moods validation ───────────────────────────────────────────────────
  if (typeof obj.moods !== 'object' || obj.moods === null || Array.isArray(obj.moods)) {
    errors.push('Missing or invalid "moods" field')
  }

  return errors
}

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
