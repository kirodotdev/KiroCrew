/**
 * Client-side image downscaling to fit the model's image limits.
 *
 * Vision models reject an entire prompt when an attached image is too large:
 *   - dimensions over ~2000px on either side in a many-image request, and
 *   - encoded payload over the per-image byte cap (~5MB).
 *
 * We downscale oversized raster images in the browser (built-in canvas) before
 * upload, so the bytes that reach the server already fit. No server-side
 * imaging dependency.
 */

/** Single source of truth for the image limits we enforce. */
export const MODEL_IMAGE_LIMITS = {
  /** Long-edge target in px. 1568 stays comfortably under the 2000px
   *  many-image dimension cap. */
  maxEdge: 1568,
  /** Max encoded bytes per image. ~4.5MB keeps margin under the ~5MB cap. */
  maxBytes: 4.5 * 1024 * 1024,
}

/** Details of an actual resize, surfaced to the UI so the user is informed. */
export interface ResizeInfo {
  name: string
  fromW: number
  fromH: number
  toW: number
  toH: number
  fromBytes: number
  toBytes: number
}

/** Result of a resize attempt. `info` is null when the file was left as-is. */
export interface ResizeResult {
  file: File
  info: ResizeInfo | null
}

/** Raster formats the canvas can decode and re-encode. SVG (vector) and GIF
 *  (animation) are intentionally excluded and passed through untouched. */
const RESIZABLE_MIME = /^image\/(png|jpeg|webp|bmp)$/i

const MIN_EDGE = 256 // floor so the shrink loop can't degrade to nothing
const MAX_ATTEMPTS = 6

type CanvasLike = OffscreenCanvas | HTMLCanvasElement

async function canvasToBlob(canvas: CanvasLike, type: string, quality?: number): Promise<Blob | null> {
  if ('convertToBlob' in canvas) {
    try {
      return await canvas.convertToBlob({ type, quality })
    } catch {
      return null
    }
  }
  return new Promise(resolve => {
    ;(canvas as HTMLCanvasElement).toBlob(b => resolve(b), type, quality)
  })
}

/** Draw the bitmap at w x h and encode to the given mime. Returns null on failure. */
async function renderToBlob(
  bitmap: ImageBitmap,
  w: number,
  h: number,
  mime: string,
  quality?: number,
): Promise<Blob | null> {
  const canvas: CanvasLike =
    typeof OffscreenCanvas !== 'undefined'
      ? new OffscreenCanvas(w, h)
      : Object.assign(document.createElement('canvas'), { width: w, height: h })
  const ctx = canvas.getContext('2d') as
    | CanvasRenderingContext2D
    | OffscreenCanvasRenderingContext2D
    | null
  if (!ctx) return null
  ctx.drawImage(bitmap, 0, 0, w, h)
  return canvasToBlob(canvas, mime, quality)
}

/** Swap a filename's extension (e.g. shot.PNG -> shot.png). */
function withExtension(name: string, ext: string): string {
  return name.replace(/\.[^./\\]+$/, '') + '.' + ext
}

/**
 * Return a downscaled copy of `file` if it is an oversized raster image (by
 * dimension or by encoded byte size), otherwise return the original unchanged.
 *
 * Shrink strategy when the image is over budget:
 *   1. Scale the long edge down to `maxEdge`.
 *   2. If the encoded bytes still exceed `maxBytes`, switch PNG -> JPEG (big
 *      win for screenshots), then step the edge down until it fits.
 *
 * Always degrades gracefully: a non-image, an undecodable file, an unsupported
 * format, a missing `createImageBitmap`, or any canvas failure returns the
 * original file so the upload still proceeds.
 */
export async function resizeImageForModel(
  file: File,
  limits: { maxEdge: number; maxBytes: number } = MODEL_IMAGE_LIMITS,
): Promise<ResizeResult> {
  const asIs: ResizeResult = { file, info: null }
  if (!RESIZABLE_MIME.test(file.type)) return asIs
  if (typeof createImageBitmap !== 'function') return asIs

  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    return asIs // undecodable in this browser — leave as-is
  }

  try {
    const { width, height } = bitmap
    const longEdge = Math.max(width, height)
    const overDim = longEdge > limits.maxEdge
    const overBytes = file.size > limits.maxBytes
    if (!overDim && !overBytes) return asIs // already within both limits

    let edge = Math.min(longEdge, limits.maxEdge)
    let useJpeg = /jpeg/i.test(file.type)
    let best: { blob: Blob; w: number; h: number; mime: string } | null = null

    for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
      const scale = edge / longEdge
      const w = Math.max(1, Math.round(width * scale))
      const h = Math.max(1, Math.round(height * scale))
      const mime = useJpeg ? 'image/jpeg' : 'image/png'
      const blob = await renderToBlob(bitmap, w, h, mime, useJpeg ? 0.9 : undefined)
      if (!blob) break
      best = { blob, w, h, mime }
      if (blob.size <= limits.maxBytes) break
      // Still too big: PNG -> JPEG first (large win), then step the edge down.
      if (!useJpeg) useJpeg = true
      else edge = Math.max(MIN_EDGE, Math.round(edge * 0.85))
    }

    // If we still couldn't get under the byte cap (e.g. attempts exhausted),
    // return the original rather than claim a resize the server will reject —
    // the server then surfaces its own meaningful error.
    if (!best || best.blob.size > limits.maxBytes) return asIs

    const out = new File(
      [best.blob],
      withExtension(file.name, best.mime === 'image/jpeg' ? 'jpg' : 'png'),
      { type: best.mime, lastModified: Date.now() },
    )
    return {
      file: out,
      info: {
        name: file.name,
        fromW: width,
        fromH: height,
        toW: best.w,
        toH: best.h,
        fromBytes: file.size,
        toBytes: out.size,
      },
    }
  } catch {
    return asIs
  } finally {
    bitmap.close?.()
  }
}
