/**
 * The crew appearance library, client side: what a pack row looks like, which
 * packs a crew can actually wear, where a slot's art is served, and whether a
 * picked file is a pack bundle at all.
 *
 * Pure data and pure functions — no React, no fetch. The library tab and the
 * avatar renderer both read from here, so "which packs are wearable" and "where
 * is the art" cannot drift between the picker and the face it picks.
 */
import type { AnimationFormat } from './types'

/**
 * The built-in pack. Its art ships INSIDE the frontend bundle (it is the
 * name-derived ghost), so the slot route answers 404 `builtin_no_content` for
 * it and every client-side path must render it locally instead of fetching.
 */
export const BUILTIN_PACK_ID = 'kiro-ghost'

/**
 * One row of `GET /api/appearances`, narrowed to what the picker renders.
 *
 * The route also returns `description` and `recoloured`; neither is read here,
 * so neither is carried. A field lands in this type with the surface that shows
 * it, which is what keeps the shape honest about what the picker actually uses.
 */
export interface AppearancePackSummary {
  id: string
  name: string
  author: string
  /** Only a custom pack can be deleted. */
  type: 'builtin' | 'custom'
  format: AnimationFormat
}

/**
 * Can a crew wear art in this format?
 *
 * Only SVG, and that is a rendering fact rather than a policy: a crew's face is
 * an `<img>`, which cannot play a Lottie document or step a sprite sheet, and
 * core ships no player for either. A pack in another format still lists — greyed
 * and unselectable — because hiding it would read as the import having failed.
 */
export const isWearableFormat = (format: string): boolean => format === 'svg'

/** Where ONE slot's art is served. `slot` is resolved server-side (working →
 *  loading → thinking → idle, done → idle, error → idle), so a pack that draws
 *  only `idle` still answers every state. */
export function packSlotUrl(id: string, slot: string): string {
  return `/api/appearances/${encodeURIComponent(id)}/slot/${encodeURIComponent(slot)}`
}

/** Total byte ceiling the server applies to a bundle (`bundle_too_large`).
 *  Checked client-side too so a mis-picked archive is refused before it is
 *  uploaded, with a message naming the limit. */
export const MAX_BUNDLE_BYTES = 24 * 1024 * 1024

/** The `kind` every exported bundle carries, and the FIRST thing the importer
 *  checks (`appearance_packs/transfer.py::import_bundle`). Named here because a
 *  bundle that reaches the server without it is refused outright, so this client
 *  must carry it through rather than rebuild an envelope of its own. */
export const PACK_BUNDLE_KIND = 'crew-companion-pack'

/** An exported pack: the ENVELOPE (`kind`, `version`, `id`) plus the manifest and
 *  every file the manifest names. This is the shape Crew Companion's gallery
 *  exports and the shape `POST /api/appearances/import` accepts.
 *
 *  The envelope is not decoration: the importer refuses a payload whose `kind` is
 *  not `PACK_BUNDLE_KIND` and takes the pack's id from `id`. An earlier version of
 *  this type held only `manifest`/`files`, which type-checked, passed this
 *  module's own tests against a mocked client, and refused every real export at
 *  the server with "That file is not a pack bundle" — a client-authored message
 *  for a bundle that was perfectly valid. Hence `[key: string]: unknown`: the
 *  envelope is the server's contract, not this module's, so unknown fields ride
 *  through instead of being dropped by a reader that does not know them yet. */
export interface PackBundle {
  kind?: unknown
  version?: unknown
  id?: unknown
  manifest: unknown
  files: Record<string, unknown>
  [key: string]: unknown
}

/** Why a picked file is not a bundle. The caller maps each to its own message —
 *  "that is not a pack file" and "that pack is too big" are different mistakes
 *  with different fixes. */
export type BundleRejection = 'unreadable' | 'too_large'

/**
 * Read a picked file's text as a pack bundle.
 *
 * A client-side check only, and deliberately shallow: it rejects what is
 * obviously not a bundle (unparseable, not a `PACK_BUNDLE_KIND` envelope, or
 * missing `manifest`/`files`) and what the server would refuse on size anyway.
 * Everything else — the id RULE, the per-file cap, the manifest's own contract,
 * whether every referenced file is carried — stays the server's decision, because
 * a second copy of those rules here would be a second thing to keep in step with
 * the importer.
 *
 * `kind` is checked here and `id` deliberately is not, though the importer needs
 * both. The line is which message serves the user: a file with no `kind` is not a
 * pack file at all, and saying so before an upload beats the same sentence after a
 * round-trip — whereas a bundle missing only its `id` earns the importer's own
 * "That bundle has an invalid pack id", which names the actual problem better than
 * this function's single rejection could.
 *
 * What it returns is the PARSED DOCUMENT, not a rebuilt one. Reconstructing the
 * envelope from fields this module happens to know is how the whole import path
 * broke once already.
 */
export function bundleFromText(
  text: string,
): { ok: true; bundle: PackBundle } | { ok: false; reason: BundleRejection } {
  if (text.length > MAX_BUNDLE_BYTES) return { ok: false, reason: 'too_large' }
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return { ok: false, reason: 'unreadable' }
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, reason: 'unreadable' }
  }
  const raw = parsed as Record<string, unknown>
  if (raw.kind !== PACK_BUNDLE_KIND) return { ok: false, reason: 'unreadable' }
  const { manifest, files } = raw
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    return { ok: false, reason: 'unreadable' }
  }
  if (!files || typeof files !== 'object' || Array.isArray(files)) {
    return { ok: false, reason: 'unreadable' }
  }
  // The document itself, envelope intact — see the note above.
  return { ok: true, bundle: raw as unknown as PackBundle }
}

/**
 * The pack rows in a `GET /api/appearances` answer.
 *
 * Total, like every other reader of a stored or served record in this tree: a
 * row missing its metadata renders as its id rather than crashing the picker,
 * and a row with no usable id is dropped — an unnameable pack cannot be
 * selected, deleted or fetched, so listing it would only offer dead controls.
 */
export function packSummariesFrom(payload: unknown): AppearancePackSummary[] {
  if (!payload || typeof payload !== 'object') return []
  const rows = (payload as { packs?: unknown }).packs
  if (!Array.isArray(rows)) return []
  const out: AppearancePackSummary[] = []
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue
    const r = row as Record<string, unknown>
    const id = typeof r.id === 'string' ? r.id : ''
    if (!id) continue
    const str = (k: string) => (typeof r[k] === 'string' ? (r[k] as string) : '')
    const format = str('format')
    out.push({
      id,
      name: str('name') || id,
      author: str('author'),
      // Anything but the built-in marker is treated as custom, which is the
      // forgiving direction: the only thing `type` gates is the delete control,
      // and the server refuses a built-in delete with 400 `builtin_pack`.
      type: r.type === 'builtin' ? 'builtin' : 'custom',
      format: (format === 'lottie' || format === 'sprite' ? format : 'svg') as AnimationFormat,
    })
  }
  return out
}
