/**
 * HEIC/HEIF photos to JPEG, in the browser, before a composer upload.
 *
 * iPhones save photos as HEIC, which no vision model and no Chromium-based
 * browser can read, and the upload boundary does not accept. Every composer
 * path (paste, file picker, drag-drop, ChatEmbed) ends in `uploadComposerFiles`,
 * which calls `prepareHeicImage` first, so all of them convert the same way and
 * the server only ever stores a JPEG. Nothing leaves the device to convert.
 *
 * Decoding tries the browser's own decoder first (Safari reads HEIC natively)
 * and only then lazy-loads the bundled libheif build from `heic-to/csp`, a
 * separate ~3 MB chunk fetched from this origin the first time a HEIC is
 * attached. That build is libheif and libde265 compiled to plain JavaScript
 * with no `eval`, and it decodes in a Worker it starts from a `blob:` URL,
 * which the dashboard CSP's `worker-src 'self' blob:` permits.
 *
 * Work is bounded before any full-size decode: the input byte cap and the
 * container's declared image size (`ispe`) are checked first, so a hostile or
 * huge file is refused without allocating its pixels.
 */
import { fmtNumber, fmtUnit } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { MODEL_IMAGE_LIMITS, renderToBlob, withExtension, type ResizeInfo } from './resizeImage'

export const HEIC_LIMITS = {
  /** Largest HEIC this converts, in bytes. */
  maxInputBytes: 50 * 1024 * 1024,
  /** Largest declared image, in pixels (64 megapixels). An iPhone 48 MP
   *  photo is under it; past it the decoded RGBA alone is over 256 MB. */
  maxPixels: 64_000_000,
} as const

/** JPEG qualities tried at each size, best first. */
const JPEG_QUALITIES = [0.92, 0.85, 0.78, 0.68] as const
/** Each step down in size keeps this share of the long edge. */
const EDGE_STEP = 0.85
/** Smallest long edge tried before giving up on the byte budget. */
const MIN_EDGE = 256

/** Why a HEIC could not be prepared. `unreadable` is the input's fault
 *  (damaged, not really HEIF); `convert_failed` is this browser's (no decoder
 *  loaded, canvas refused); `cannot_fit` is the output budget's. */
export type HeicFailure = 'unreadable' | 'too_large' | 'too_many_pixels' | 'convert_failed' | 'cannot_fit'

export class HeicPrepareError extends Error {
  readonly kind: HeicFailure
  readonly fileName: string
  constructor(kind: HeicFailure, fileName: string, message: string) {
    super(message)
    this.name = 'HeicPrepareError'
    this.kind = kind
    this.fileName = fileName
  }
}

const HEIC_MIME = /^image\/hei[cf](?:-sequence)?$/i
const HEIC_EXT = /\.hei[cf]$/i
/** Types a browser reports when it does not know the file's type. */
const GENERIC_MIME = new Set(['', 'application/octet-stream'])
/** ISO-BMFF major brands of HEVC-coded HEIF. */
const HEVC_BRANDS = new Set(['heic', 'heix', 'hevc', 'hevx', 'heim', 'heis'])
/** Generic still-image brands: HEVC-coded only when a compatible brand says so
 *  (AVIF files carry them too, and browsers decode AVIF natively). */
const GENERIC_HEIF_BRANDS = new Set(['mif1', 'msf1'])
/** How much of a file the brand sniff reads. */
const SNIFF_BYTES = 64

/** By declared type or name alone: `image/heic`/`image/heif`, or a `.heic`/
 *  `.heif` name (any case) when the type is empty or generic. */
export function looksLikeHeic(file: File): boolean {
  if (HEIC_MIME.test(file.type)) return true
  return GENERIC_MIME.has(file.type.toLowerCase()) && HEIC_EXT.test(file.name)
}

function fourcc(b: Uint8Array, at: number): string {
  return String.fromCharCode(b[at], b[at + 1], b[at + 2], b[at + 3])
}

/** True when `head` is an HEVC-coded HEIF `ftyp`, whatever the file claims. */
export function isHeicSignature(head: Uint8Array): boolean {
  if (head.length < 16 || fourcc(head, 4) !== 'ftyp') return false
  const major = fourcc(head, 8)
  if (HEVC_BRANDS.has(major)) return true
  if (!GENERIC_HEIF_BRANDS.has(major)) return false
  const boxEnd = Math.min(head.length, new DataView(head.buffer, head.byteOffset, head.byteLength).getUint32(0))
  for (let at = 16; at + 4 <= boxEnd; at += 4) {
    if (HEVC_BRANDS.has(fourcc(head, at))) return true
  }
  return false
}

/** Whether `file` must be converted: claimed HEIC, or HEIC bytes under another
 *  image name (an iPhone photo saved as `IMG_1234.jpeg` is routinely one). */
export async function needsHeicConversion(file: File): Promise<boolean> {
  if (looksLikeHeic(file)) return true
  const type = file.type.toLowerCase()
  if (!GENERIC_MIME.has(type) && !type.startsWith('image/')) return false
  try {
    return isHeicSignature(new Uint8Array(await file.slice(0, SNIFF_BYTES).arrayBuffer()))
  } catch {
    return false
  }
}

interface Box { type: string; start: number; body: number; end: number }

/** The ISO-BMFF boxes in `b[from, to)`; stops at the first malformed header. */
function boxes(b: Uint8Array, from: number, to: number): Box[] {
  const view = new DataView(b.buffer, b.byteOffset, b.byteLength)
  const out: Box[] = []
  let at = from
  while (at + 8 <= to) {
    let size = view.getUint32(at)
    let header = 8
    if (size === 1) {
      if (at + 16 > to) break
      const big = view.getBigUint64(at + 8)
      if (big > BigInt(to - at)) break
      size = Number(big)
      header = 16
    } else if (size === 0) {
      size = to - at
    }
    if (size < header || at + size > to) break
    out.push({ type: fourcc(b, at + 4), start: at, body: at + header, end: at + size })
    at += size
  }
  return out
}

/** The largest image size the HEIF container declares (its `ispe` properties),
 *  or null when there is none to read. Every HEIF image item must carry an
 *  `ispe`, so a file without one is not a readable HEIF. Only box headers are
 *  read; no pixel data is touched. */
export function heifDeclaredSize(b: Uint8Array): { width: number; height: number } | null {
  const meta = boxes(b, 0, b.length).find(x => x.type === 'meta')
  if (!meta) return null
  // `meta` is a FullBox: 4 bytes of version and flags before its children.
  const iprp = boxes(b, meta.body + 4, meta.end).find(x => x.type === 'iprp')
  const ipco = iprp && boxes(b, iprp.body, iprp.end).find(x => x.type === 'ipco')
  if (!ipco) return null
  const view = new DataView(b.buffer, b.byteOffset, b.byteLength)
  let best: { width: number; height: number } | null = null
  for (const prop of boxes(b, ipco.body, ipco.end)) {
    // `ispe` is a FullBox too: version/flags, then 32-bit width and height.
    if (prop.type !== 'ispe' || prop.body + 12 > prop.end) continue
    const width = view.getUint32(prop.body + 4)
    const height = view.getUint32(prop.body + 8)
    if (!best || width * height > best.width * best.height) best = { width, height }
  }
  return best
}

export type HeicDecoder = (file: File) => Promise<ImageBitmap>

/** Load the bundled libheif decoder. A separate step from decoding, so a chunk
 *  that never arrives is reported as this browser's failure, not the file's.
 *  libheif applies the orientation transforms (`irot`/`imir`), so the bitmap
 *  it returns is upright. */
async function loadLibheif(): Promise<HeicDecoder> {
  const { heicTo } = await import('heic-to/csp')
  return file => heicTo({ blob: file, type: 'bitmap' })
}

export interface HeicPrepared {
  file: File
  /** Set when the JPEG is smaller than the photo, as the resize chip shows. */
  info: ResizeInfo | null
}

function message(kind: HeicFailure, name: string, values: Record<string, string>): string {
  switch (kind) {
    case 'unreadable': return i18nT('utils.heicImage.unreadable', { name })
    case 'too_large': return i18nT('utils.heicImage.too_large', { name, ...values })
    case 'too_many_pixels': return i18nT('utils.heicImage.too_many_pixels', { name, ...values })
    case 'convert_failed': return i18nT('utils.heicImage.convert_failed', { name })
    case 'cannot_fit': return i18nT('utils.heicImage.cannot_fit', { name, ...values })
  }
}

/** A byte limit in the binary megabytes the server's own refusals quote. */
function mebibytes(bytes: number): string {
  return fmtUnit(bytes / 1024 / 1024, 'megabyte', { maximumFractionDigits: 1 })
}

function refusal(kind: HeicFailure, file: File, values: Record<string, string> = {}): HeicPrepareError {
  return new HeicPrepareError(kind, file.name, message(kind, file.name, values))
}

/** Decode with the browser's own HEIC support when it has one (Safari), else
 *  with libheif. */
async function decode(file: File, loadDecoder: () => Promise<HeicDecoder>): Promise<ImageBitmap> {
  if (typeof createImageBitmap === 'function') {
    try {
      return await createImageBitmap(file)
    } catch {
      // No native HEIC decoder here (every Chromium build, Firefox).
    }
  }
  let decoder: HeicDecoder
  try {
    decoder = await loadDecoder()
  } catch {
    throw refusal('convert_failed', file)
  }
  try {
    return await decoder(file)
  } catch {
    throw refusal('unreadable', file)
  }
}

/**
 * Convert a HEIC/HEIF `file` to a JPEG `File` that fits the model's image
 * limits, or return `null` when it is not HEIC and needs no conversion.
 *
 * The JPEG is named after the photo with a `.jpg` extension, typed
 * `image/jpeg`, upright, and no larger than `limits` on its long edge or in
 * bytes. Sizing starts at the photo's own size capped to the long-edge limit
 * and steps qualities 0.92 to 0.68, then smaller sizes, until it fits.
 *
 * Throws `HeicPrepareError` with a sentence for the user when the file is too
 * large, declares too many pixels, cannot be decoded, or cannot be made small
 * enough.
 */
export async function prepareHeicImage(
  file: File,
  limits: { maxEdge: number; maxBytes: number } = MODEL_IMAGE_LIMITS,
  loadDecoder: () => Promise<HeicDecoder> = loadLibheif,
): Promise<HeicPrepared | null> {
  if (!(await needsHeicConversion(file))) return null
  if (file.size > HEIC_LIMITS.maxInputBytes) {
    throw refusal('too_large', file, { max: mebibytes(HEIC_LIMITS.maxInputBytes) })
  }
  let bytes: Uint8Array
  try {
    bytes = new Uint8Array(await file.arrayBuffer())
  } catch {
    throw refusal('unreadable', file)
  }
  const declared = heifDeclaredSize(bytes)
  if (!declared || declared.width < 1 || declared.height < 1) throw refusal('unreadable', file)
  if (declared.width * declared.height > HEIC_LIMITS.maxPixels) {
    throw refusal('too_many_pixels', file, {
      width: fmtNumber(declared.width),
      height: fmtNumber(declared.height),
      max: fmtNumber(HEIC_LIMITS.maxPixels / 1_000_000),
    })
  }

  const bitmap = await decode(file, loadDecoder)
  try {
    const { width, height } = bitmap
    const longEdge = Math.max(width, height)
    let edge = Math.min(longEdge, limits.maxEdge)
    for (;;) {
      const scale = edge / longEdge
      const w = Math.max(1, Math.round(width * scale))
      const h = Math.max(1, Math.round(height * scale))
      for (const quality of JPEG_QUALITIES) {
        const blob = await renderToBlob(bitmap, w, h, 'image/jpeg', quality)
        if (!blob) throw refusal('convert_failed', file)
        if (blob.size <= limits.maxBytes) {
          const out = new File([blob], withExtension(file.name, 'jpg'), { type: 'image/jpeg', lastModified: Date.now() })
          const resized = w !== width || h !== height
          return {
            file: out,
            info: resized
              ? { name: file.name, fromW: width, fromH: height, toW: w, toH: h, fromBytes: file.size, toBytes: out.size }
              : null,
          }
        }
      }
      if (edge <= MIN_EDGE) break
      edge = Math.max(MIN_EDGE, Math.round(edge * EDGE_STEP))
    }
    throw refusal('cannot_fit', file, { max: mebibytes(limits.maxBytes) })
  } catch (err) {
    // A canvas that throws (rather than returning null) is this browser's
    // failure; a refusal raised above passes through unchanged.
    if (err instanceof HeicPrepareError) throw err
    throw refusal('convert_failed', file)
  } finally {
    bitmap.close?.()
  }
}
