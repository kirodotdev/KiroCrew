/**
 * Petdex.dev import — the sheet convention, and the prefill it produces.
 *
 * A petdex pet is a spritesheet plus a `pet.json` that carries only
 * `id` / `displayName` / `description` / `spritesheetPath`. There is **no state
 * mapping in the file**: which row drives which animation is a positional
 * convention. That is why import ends in the sprite importer with every row
 * previewed and every slot a dropdown — this module supplies the DEFAULTS, and
 * the user confirms or overrides them. A silently mis-mapped pet (walking on
 * the "failed" row) is the failure mode being designed out.
 *
 * Verified against the published assets (2026-07): community pets are
 * 1536x1872 = 8 cols x 9 rows of 192x208. A curated pet was observed at
 * 1536x2288 (11 rows) carrying `spriteVersionNumber: 2`, a layout the upstream
 * open-source client does not yet handle (it hard-codes 9 rows). So the grid is
 * derived from the PIXELS rather than from a row constant, and any rows past
 * the known nine simply show up as extra choices in the importer.
 */
import { i18nT } from '../../i18n/t'

/** Frame box every petdex sheet uses. Constant across both observed layouts. */
export const PETDEX_FRAME_W = 192
export const PETDEX_FRAME_H = 208

/**
 * Playback rate for imported pets.
 *
 * Upstream times each row separately (idle is 6 frames over ~1100ms, the run
 * rows are quicker). Our `SpriteConfig` carries ONE fps for the whole pack, so
 * this is a deliberate compromise near the middle of that range; the importer
 * exposes an FPS field for anyone who wants to tune it.
 */
export const PETDEX_FPS = 8

/**
 * The row order, from the upstream client's own state table.
 *
 * `frames` is how many of the 8 columns that row actually fills. Nothing needs
 * it to slice correctly — `SpriteRenderer` already skips empty trailing frames
 * at render time — so it is here to label the previews honestly.
 */
export interface PetdexRow {
  index: number
  id: string
  frames: number
}

export const PETDEX_ROWS: readonly PetdexRow[] = [
  { index: 0, id: 'idle', frames: 6 },
  { index: 1, id: 'running-right', frames: 8 },
  { index: 2, id: 'running-left', frames: 8 },
  { index: 3, id: 'waving', frames: 4 },
  { index: 4, id: 'jumping', frames: 5 },
  { index: 5, id: 'failed', frames: 8 },
  { index: 6, id: 'waiting', frames: 6 },
  { index: 7, id: 'running', frames: 6 },
  { index: 8, id: 'review', frames: 6 },
]

/**
 * Default Mochi-slot -> petdex-row mapping.
 *
 * Notes on the non-obvious ones:
 * - `walking` takes `running-right` and NOT `running-left`: the pet flips
 *   horizontally to walk the other way (`flipX`), so the left row is redundant
 *   and stays free for the user to assign if they prefer hand-drawn turns.
 * - `working` takes `running`, which in an agent pet means "running a tool",
 *   while `thinking` takes `review` ("reading the code").
 * - `offline` takes `waiting`, the only near-static row available.
 * Every row except `running-left` is used by some slot, so a user who changes
 * nothing still gets a pet that animates differently in every state.
 */
export const PETDEX_SLOT_ROWS: Readonly<Record<string, number>> = {
  idle: 0,
  walking: 1,
  thinking: 8,
  working: 7,
  error: 5,
  offline: 6,
  peeking: 3,
  peekThinking: 8,
  happy: 3,
  curious: 4,
  busy: 7,
  sleepy: 6,
  scared: 5,
}

/** What the backend returns for one pet, from either source. */
export interface PetdexPet {
  slug: string
  meta: { id?: string; displayName?: string; description?: string; spritesheetPath?: string }
  imageMime: string
  imageBase64: string
  source: string
}

/** One entry in the "already installed" picker. */
export interface PetdexInstalled {
  slug: string
  name: string
  description: string
}

export interface PetdexGrid {
  frameWidth: number
  frameHeight: number
  cols: number
  rows: number
  /** True when the sheet divides evenly into the documented 192x208 grid. */
  matchesConvention: boolean
}

/**
 * Derive the grid from the sheet's pixel size.
 *
 * Returns `matchesConvention: false` when the sheet does not divide evenly into
 * 192x208 boxes (a hand-made or resized sheet). Callers should then fall back to
 * the importer's own frame-size detection instead of forcing this grid —
 * imposing the wrong box silently shears every frame.
 */
export function derivePetdexGrid(width: number, height: number): PetdexGrid {
  const evenCols = width > 0 && width % PETDEX_FRAME_W === 0
  const evenRows = height > 0 && height % PETDEX_FRAME_H === 0
  const cols = evenCols ? width / PETDEX_FRAME_W : 0
  const rows = evenRows ? height / PETDEX_FRAME_H : 0
  return {
    frameWidth: PETDEX_FRAME_W,
    frameHeight: PETDEX_FRAME_H,
    cols,
    rows,
    // At least the documented 8x9: fewer rows than the convention means the
    // row indices below would point past the end of the sheet.
    matchesConvention: evenCols && evenRows && cols >= 8 && rows >= PETDEX_ROWS.length,
  }
}

/** A data URI the browser can decode, with the real mime from the backend. */
export function petdexImageUri(pet: PetdexPet): string {
  return `data:${pet.imageMime};base64,${pet.imageBase64}`
}

export interface SpritePrefill {
  name: string
  author: string
  description: string
  imageUri: string
  frameWidth: number
  frameHeight: number
  fps: number
  /** Mochi slot -> row index. Absent slots stay unassigned. */
  rowAssignments: Record<string, number>
}

/**
 * Build the sprite-importer prefill for a fetched pet.
 *
 * When the sheet does not match the convention, the geometry and the row
 * assignments are BOTH omitted: a mapping computed from a grid we could not
 * confirm would point at the wrong art, and the importer's detection plus the
 * user's own eyes are a better answer than a confident guess.
 */
export function petdexPrefill(pet: PetdexPet, width: number, height: number): SpritePrefill {
  const grid = derivePetdexGrid(width, height)
  const name = pet.meta.displayName || pet.meta.id || pet.slug
  const base: SpritePrefill = {
    name,
    author: 'petdex.dev',
    description: pet.meta.description || i18nT('apps.mochi.petdex.imported_desc', { slug: pet.slug }),
    imageUri: petdexImageUri(pet),
    frameWidth: grid.frameWidth,
    frameHeight: grid.frameHeight,
    fps: PETDEX_FPS,
    rowAssignments: {},
  }
  if (!grid.matchesConvention) {
    return { ...base, frameWidth: 0, frameHeight: 0 }
  }
  const rowAssignments: Record<string, number> = {}
  for (const [slot, row] of Object.entries(PETDEX_SLOT_ROWS)) {
    if (row < grid.rows) rowAssignments[slot] = row
  }
  return { ...base, rowAssignments }
}
