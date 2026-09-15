/**
 * A pack's DETAIL answer — the whole pack, as `GET /api/appearances/{id}` serves
 * it — plus the two things a client needs to draw from it: which slot a state
 * actually resolves to, and which sprite row that slot occupies.
 *
 * Separate from `library.ts` because the two answer different questions. That
 * module is about the LIST (what a crew may wear, where one slot's bytes are);
 * this one is about ONE pack's contents, which only a renderer reads.
 *
 * Pure data and pure functions — no React, no fetch.
 */
import type { AnimationFormat } from './types'

/** One slot's art: the format that decides which player draws it, and the
 *  bytes ONLY when that player reads them from here. `lottie` does — the
 *  document is parsed in the page. An `svg` slot is drawn by an `<img>` on the
 *  slot route and a `sprite` sheet by a canvas from the same route (base64 text
 *  is not an image), so for those two `content` is `''`: the detail route
 *  inlines every file, and keeping bytes no renderer reads would pin a whole
 *  base64 sheet in the query cache for the session. */
export interface PackSlotArt {
  content: string
  format: AnimationFormat
}

/**
 * `GET /api/appearances/{id}`, narrowed to what a renderer reads.
 *
 * `categories` and `randomNames` are served too and are deliberately absent:
 * nothing here draws a category list. A field lands in this type with the
 * surface that shows it.
 */
export interface PackDetail {
  animations: Record<string, PackSlotArt>
  sprite?: PackSpriteConfig
}

/**
 * The sprite geometry a renderer may TRUST. Every field is optional because
 * each is dropped rather than kept when the served value is not a positive
 * finite number: the config comes out of a third-party bundle verbatim (the
 * store keeps `manifest.sprite` as written), and `SpriteRenderer` divides the
 * sheet's width by `frameWidth` to count frames — a `"0"` there is `Infinity`
 * frames and a shrink loop that never ends, on the main thread. A field that
 * is absent takes the renderer's own default instead.
 */
export interface PackSpriteConfig {
  /** Whole pixels, at least 1. */
  frameWidth?: number
  /** Whole pixels, at least 1. */
  frameHeight?: number
  /** Positive and finite. */
  fps?: number
  rowAssignments?: Record<string, number>
}

/** A frame dimension: a whole number of pixels, at least one. Anything else —
 *  a string, `0`, a negative, `NaN`, a fraction — is "not given". A fraction is
 *  dropped rather than floored: `16.5` cuts the sheet at 16 as surely as at 17,
 *  and a config the author got wrong is better replaced by the renderer's
 *  default than silently rounded into a different wrong answer. */
function frameDimension(raw: unknown): number | undefined {
  return Number.isInteger(raw) && (raw as number) >= 1 ? (raw as number) : undefined
}

/** A frame rate: any positive finite number. */
function frameRate(raw: unknown): number | undefined {
  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw <= 0) return undefined
  return raw
}

function spriteConfigFrom(raw: unknown): PackSpriteConfig | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined
  const r = raw as Record<string, unknown>
  const rows = r.rowAssignments
  return {
    frameWidth: frameDimension(r.frameWidth),
    frameHeight: frameDimension(r.frameHeight),
    fps: frameRate(r.fps),
    rowAssignments:
      rows && typeof rows === 'object' && !Array.isArray(rows)
        ? (rows as Record<string, number>)
        : undefined,
  }
}

/**
 * The slots a state falls back through, in order — the SAME chain
 * `dashboard/appearances.py::pack_slot_file` resolves server-side, so the format
 * a client picks a player for is the format the bytes will actually be.
 *
 * Any other name resolves to itself only: a pack's random clips are named by
 * their author, so a fixed vocabulary would make them unresolvable.
 */
const FALLBACKS: Record<string, readonly string[]> = {
  working: ['working', 'loading', 'thinking', 'idle'],
  done: ['done', 'idle'],
  error: ['error', 'idle'],
}

/**
 * Which slot this pack really draws for `state`, or `null` when it draws none
 * even after fallback (the server's 404 `slot_not_found`).
 *
 * Client-side because the DISPATCH needs it: the format lives on the resolved
 * slot, and a player has to be chosen before any bytes are requested. The chain
 * is duplicated rather than inferred from `X-Resolved-Slot`, because that header
 * arrives with the art and the choice of player has to be made before the
 * request — but it is duplicated ONCE, here, and pinned by a test that reads the
 * same order out of the backend module.
 */
export function resolveSlot(detail: PackDetail, state: string): string | null {
  const chain = FALLBACKS[state] ?? [state]
  for (const slot of chain) {
    if (detail.animations[slot]) return slot
  }
  return null
}

/**
 * Which sprite ROW a resolved slot occupies. `rowAssignments` is the authoring
 * map (`{idle: 0, working: 1}`); a sheet with no entry for this slot is a
 * single-row strip, which is row 0 — the same answer the Companion's own
 * importer writes for a one-row sheet.
 *
 * Negative and non-integer rows are floored to 0 rather than trusted: the map
 * comes out of a third-party bundle, and a negative offset would sample above
 * the sheet and draw nothing at all.
 */
export function spriteRowFor(detail: PackDetail, slot: string): number {
  const raw = detail.sprite?.rowAssignments?.[slot]
  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw < 0) return 0
  return Math.floor(raw)
}

/**
 * Read a detail answer.
 *
 * Total, like every other reader of a served record in this tree: a pack whose
 * payload is junk resolves to "no art", which the renderer reports as a load
 * failure and the avatar answers with the seeded ghost — the same thing a
 * missing pack already shows. A slot whose entry is malformed is DROPPED rather
 * than defaulted, so the fallback chain steps past it instead of handing a
 * player bytes it cannot read.
 */
export function packDetailFrom(payload: unknown): PackDetail {
  const empty: PackDetail = { animations: {} }
  if (!payload || typeof payload !== 'object') return empty
  const p = payload as Record<string, unknown>
  const animations: Record<string, PackSlotArt> = {}
  const raw = p.animations
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    for (const [slot, entry] of Object.entries(raw as Record<string, unknown>)) {
      if (!entry || typeof entry !== 'object') continue
      const e = entry as Record<string, unknown>
      if (typeof e.content !== 'string' || !e.content) continue
      const format = e.format
      // An unknown format is read as `svg`, the forgiving direction and the one
      // the listing already takes: the slot route serves it as an image, and an
      // `<img>` that cannot decode it falls back to the ghost. Choosing a PLAYER
      // on a guess would instead hand a Lottie parser a PNG.
      const kept = (format === 'lottie' || format === 'sprite' ? format : 'svg') as AnimationFormat
      animations[slot] = {
        // The bytes stay only for the player that parses them here (see
        // PackSlotArt). The emptiness check above still ran on the served
        // value, so a slot with no art is dropped rather than kept blank.
        content: kept === 'lottie' ? e.content : '',
        format: kept,
      }
    }
  }
  return { animations, sprite: spriteConfigFrom(p.sprite) }
}
