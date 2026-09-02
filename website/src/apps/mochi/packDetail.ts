/**
 * Pack detail in the shape the RENDERERS consume — ONE builder, both surfaces.
 *
 * A stored manifest's `states`/`moods` values are FILENAMES; every renderer
 * (`PetWidget`'s `buildResolverFromPackDetail`, the Avatars grid's
 * `AnimThumbnail`) reads `animations[slot].content` instead. Flattening one into
 * the other is what this module does.
 *
 * It exists because that flattening was implemented in the mochiApi seam ONLY.
 * The pet's live appearance-switch path (`petBridge.onGalleryActiveChanged`) built
 * its payload from the RAW manifest, so `data.animations` was undefined and
 * `PetWidget`'s handler — which is written `if (data?.meta && data?.animations)` —
 * silently did nothing. The pet kept its previous art (the compiled-in orange cat
 * whenever no resolver was set), so applying a custom pack "did nothing" until the
 * app was restarted, because only the MOUNT path went through the seam. Two
 * builders for one payload shape is the bug; there is now one.
 *
 * Built-ins are handled here too: their art is compiled into this bundle, so the
 * packs route would 404 for them.
 */
import { builtinPackDetail } from './builtinPacks'
import type { InlineAnimation, PackDetail } from './builtinPacks'
import { galleryGetPackDetail as readManifest, galleryPackFileUrl } from './panel/panelBridge'

/** Sprite MIME by extension — the data URI must declare the real type. */
const SPRITE_MIME: Record<string, string> = {
  png: 'image/png',
  webp: 'image/webp',
  gif: 'image/gif',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
}

/**
 * Animation format from a pack filename.
 *
 * Sprite sheets used to fall into the `svg` branch (the test was only
 * `.json ? lottie : svg`), so a `.webp` sheet was read as TEXT and handed to
 * `toDataUri` — every slot of an imported sprite pack rendered as a broken
 * image, with nothing logged. Petdex packs are webp by default, so this was
 * every pack imported from there.
 */
export function packFileFormat(filename: string): InlineAnimation['format'] {
  const ext = filename.split('.').pop()?.toLowerCase() ?? ''
  if (ext === 'json') return 'lottie'
  if (ext === 'svg') return 'svg'
  return ext in SPRITE_MIME ? 'sprite' : 'svg'
}

/**
 * Pack file bytes in the shape the renderers consume.
 *
 * Text for svg/lottie; a `data:` URI for a sprite sheet. `res.text()` on binary
 * decodes it as UTF-8 and destroys it, so the sheet is read as a blob and
 * base64-encoded with the MIME its extension declares. Every consumer already
 * passes a `data:` value straight through.
 */
export async function packFileContent(res: Response, filename: string): Promise<string> {
  if (packFileFormat(filename) !== 'sprite') return res.text()
  const ext = filename.split('.').pop()?.toLowerCase() ?? ''
  const mime = SPRITE_MIME[ext] ?? 'image/png'
  const buf = new Uint8Array(await res.arrayBuffer())
  let binary = ''
  // Chunked so a large sheet cannot blow the argument limit of fromCharCode.
  for (let i = 0; i < buf.length; i += 0x8000) {
    binary += String.fromCharCode(...buf.subarray(i, i + 0x8000))
  }
  return `data:${mime};base64,${btoa(binary)}`
}

/**
 * A pack's manifest PLUS its art inlined as `animations[slot]`.
 *
 * Built-in first (its art is in this bundle), then the stored manifest with one
 * fetch per referenced slot. Returns null when the pack has no readable
 * manifest, so a caller can say WHICH pack failed rather than rendering blank.
 */
export async function resolvePackDetail(packId: string): Promise<PackDetail | null> {
  const builtin = builtinPackDetail(packId)
  if (builtin !== null) return builtin
  const manifest = await readManifest(packId)
  if (manifest === null || manifest === undefined) return null
  const slots: Record<string, string> = {
    ...((manifest.states ?? {}) as unknown as Record<string, string>),
    ...((manifest.moods ?? {}) as unknown as Record<string, string>),
  }
  const animations: Record<string, InlineAnimation> = {}
  await Promise.all(
    Object.entries(slots).map(async ([slot, filename]) => {
      if (typeof filename !== 'string' || filename === '') return
      try {
        const res = await fetch(galleryPackFileUrl(packId, filename), {
          credentials: 'same-origin',
        })
        if (!res.ok) return
        animations[slot] = {
          content: await packFileContent(res, filename),
          format: packFileFormat(filename),
        }
      } catch {
        // One unreadable slot must not blank the whole pack.
      }
    }),
  )
  return { ...manifest, animations }
}
