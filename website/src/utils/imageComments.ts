/**
 * Box comments on rendered markdown images.
 *
 * A box records WHERE on an image a comment points as fractions (0..1) of the
 * rendered image, never as pixels, so it covers the same part of the picture
 * whatever size or pixel density the image is shown at. The host (the file
 * viewer) owns the comments; the markdown renderer only reads them through
 * `ImageCommentCtx` and reports new boxes back. With no provider, as in the
 * chat transcript, images render exactly as they always did.
 */
import { createContext, type CSSProperties } from 'react'
import { pathBasename, stripTrailingSeparators } from './pathBasename'

/** A box on an image as fractions of it: `x`/`y` its top-left corner from the
 *  image's top-left, `w`/`h` its size, all in 0..1 and inside the image. */
export interface ImageBoxRect {
  x: number
  y: number
  w: number
  h: number
}

/** An existing comment's box, as the renderer draws it. */
export interface ImageBox extends ImageBoxRect {
  id: string
  /** 1-based position in the pending comment list, the number the box shows. */
  number: number
  /** The comment text, shown as the box's tooltip. */
  text: string
}

/** A box the reader drew on an image, handed to the host to open its composer. */
export interface ImageBoxPlacement extends ImageBoxRect {
  /** The image destination exactly as written in the markdown, not the resolved fetch URL. */
  src: string
  /** The file the preview loaded for `src`, decoded (a `<a b.png>` destination
   *  is `a b.png` here, not `a%20b.png`), for the agent payload. */
  path?: string
  alt?: string
  /** 1-based source line (and column) of the image in the rendered markdown. */
  line?: number
  column?: number
  /** Viewport coordinates where the composer opens: where the drag ended. */
  clientX: number
  clientY: number
}

/** The box whose composer is open, drawn as a provisional outline. */
export interface ImageBoxDraft extends ImageBoxRect {
  src: string
  line?: number
  column?: number
  /** Restored after the panel was torn down, so it has no composer position
   *  yet: the image that owns it reports one through `placeDraft`. */
  unplaced?: boolean
}

export interface ImageCommentHost {
  /** Boxes belonging to the image at `src` on source `line` and `column`. */
  boxesFor: (src: string, line: number | undefined, column: number | undefined) => ImageBox[]
  /** Record that an image is rendered at `src`, `line`, `column`, so `boxesFor`
   *  can tell one occurrence from another after an edit moves them. Returns the
   *  call that removes the record. Must be a stable function. */
  register: (src: string, line: number | undefined, column: number | undefined) => () => void
  draft: ImageBoxDraft | null
  /** True when `draft` belongs to the image at `src`, `line`, `column`. The
   *  host decides, as it does for saved boxes, so a draft kept while the panel
   *  was torn down still finds its image after an edit moved it. */
  ownsDraft: (src: string, line: number | undefined, column: number | undefined) => boolean
  onPlace: (placement: ImageBoxPlacement) => void
  /** Where the composer of an `unplaced` draft opens, in viewport coordinates:
   *  the box's far corner, where the drag that drew it ended. */
  placeDraft?: (clientX: number, clientY: number) => void
  /** A box was clicked: reveal its comment, as a click on highlighted text does. */
  onActivate: (id: string) => void
  /** The pointer is over a box (`text` is its comment) or has left it (`null`):
   *  show or hide the popover a hover on highlighted text shows. `box` is the
   *  box's rect on screen, which the popover sits beside rather than over. */
  onHover: (text: string | null, clientX: number, clientY: number, box?: PlainRect) => void
}

export const ImageCommentCtx = createContext<ImageCommentHost | null>(null)

/** A comment on the image as a whole, for a reader who cannot drag. */
export const WHOLE_IMAGE: ImageBoxRect = { x: 0, y: 0, w: 1, h: 1 }

/** Whether `b` is the box `WHOLE_IMAGE` placed: one covering the whole picture. */
export function isWholeImage(b: ImageBoxRect): boolean {
  return b.x === 0 && b.y === 0 && b.w === 1 && b.h === 1
}

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v)

export interface PlainRect { left: number; top: number; width: number; height: number }

/** The gap between a box and the popover beside it, and the popover's least
 *  distance from the window's edge, in CSS pixels. */
const POPOVER_GAP_PX = 8
const POPOVER_MARGIN_PX = 6

/**
 * Where a box's comment popover goes so it doesn't cover the box: to its right,
 * else its left, level with its top; else below it, else above it, level with
 * its left; kept inside the window. Null when the box leaves no room on any
 * side, where the popover follows the pointer instead.
 */
export function popoverBesideBox(box: PlainRect, size: { width: number; height: number }, view: { width: number; height: number }): { left: number; top: number } | null {
  const fits = (start: number, length: number, limit: number) => start >= POPOVER_MARGIN_PX && start + length <= limit - POPOVER_MARGIN_PX
  const within = (start: number, length: number, limit: number) => Math.max(POPOVER_MARGIN_PX, Math.min(start, limit - length - POPOVER_MARGIN_PX))
  const right = box.left + box.width + POPOVER_GAP_PX
  const left = box.left - POPOVER_GAP_PX - size.width
  const below = box.top + box.height + POPOVER_GAP_PX
  const above = box.top - POPOVER_GAP_PX - size.height
  if (fits(right, size.width, view.width)) return { left: right, top: within(box.top, size.height, view.height) }
  if (fits(left, size.width, view.width)) return { left, top: within(box.top, size.height, view.height) }
  if (fits(below, size.height, view.height)) return { left: within(box.left, size.width, view.width), top: below }
  if (fits(above, size.height, view.height)) return { left: within(box.left, size.width, view.width), top: above }
  return null
}

/**
 * Where the picture is drawn inside an image element's box. With `object-fit:
 * contain` (every markdown image) the element and the picture match while the
 * image keeps its aspect ratio, but an image given a fixed width, as an SVG
 * is, keeps that width when `max-height` caps it, and the picture is centered
 * between empty bands. A box is a region of the picture, so its fractions are
 * taken here, not on the element. The element's own rect when the image has no
 * natural size yet or the fit does not letterbox.
 */
export function pictureRect(rect: PlainRect, natural: { width: number; height: number }, objectFit: string): PlainRect {
  const { width: nw, height: nh } = natural
  if (!(nw > 0) || !(nh > 0) || !(rect.width > 0) || !(rect.height > 0)) return rect
  if (objectFit !== 'contain' && objectFit !== 'scale-down') return rect
  let scale = Math.min(rect.width / nw, rect.height / nh)
  if (objectFit === 'scale-down') scale = Math.min(scale, 1)
  const width = nw * scale
  const height = nh * scale
  // object-position stays at its 50% 50% default for markdown images.
  return { left: rect.left + (rect.width - width) / 2, top: rect.top + (rect.height - height) / 2, width, height }
}

/** pictureRect for a live image element, in viewport coordinates, or in its
 *  offsetParent's when `offset` is set. */
export function pictureRectOf(img: HTMLImageElement, offset = false): PlainRect {
  const rect = offset
    ? { left: img.offsetLeft, top: img.offsetTop, width: img.offsetWidth, height: img.offsetHeight }
    : img.getBoundingClientRect()
  const fit = typeof getComputedStyle === 'function' ? getComputedStyle(img).objectFit : ''
  return pictureRect(rect, { width: img.naturalWidth, height: img.naturalHeight }, fit)
}

/**
 * The box spanned by two viewport points, as fractions of `rect` (the image's
 * bounding box). Either corner can come first, and a drag that leaves the image
 * is clamped to its edge. Null when the image has no area.
 */
export function dragToImageBox(
  from: { x: number; y: number },
  to: { x: number; y: number },
  rect: { left: number; top: number; width: number; height: number },
): ImageBoxRect | null {
  if (!(rect.width > 0) || !(rect.height > 0)) return null
  const fx = (v: number) => clamp01((v - rect.left) / rect.width)
  const fy = (v: number) => clamp01((v - rect.top) / rect.height)
  const x0 = fx(Math.min(from.x, to.x))
  const x1 = fx(Math.max(from.x, to.x))
  const y0 = fy(Math.min(from.y, to.y))
  const y1 = fy(Math.max(from.y, to.y))
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }
}

/** Round to 0.01%, far below a pixel on any real image, so float noise
 *  (0.328 * 100 = 32.800000000000004) never reaches a style attribute. */
const hundredths = (v: number) => Math.round(clamp01(v) * 10000) / 100

/**
 * Absolute position for a box as percentages of the layer that tracks the image,
 * so a resize moves the box with the image without re-measuring. Physical
 * `left` on purpose: the box is a region of the picture, which does not mirror
 * in a right-to-left layout.
 */
export function boxPositionStyle(box: ImageBoxRect): CSSProperties {
  return {
    left: `${hundredths(box.x)}%`,
    top: `${hundredths(box.y)}%`,
    width: `${hundredths(box.w)}%`,
    height: `${hundredths(box.h)}%`,
  }
}

/** True when a box recorded for `box` belongs to the image rendered at
 *  `src`/`line`/`column`. The line and column separate two occurrences of the
 *  same image in one document, including two on one line; a box, or an image,
 *  with no line (or column) resolved matches on what is known. */
export function boxMatchesImage(
  box: { src: string; line?: number; column?: number },
  src: string,
  line: number | undefined,
  column: number | undefined,
): boolean {
  if (box.src !== src) return false
  if (box.line != null && line != null && box.line !== line) return false
  return box.column == null || column == null || box.column === column
}

/** Where one image is rendered in the document now. */
export interface ImageSpot {
  line?: number
  column?: number
}

/** Which use of its file a box was drawn on: the `nth` (0-based) of `uses`
 *  images with the same `src`, in document order. */
export interface ImageUse {
  nth: number
  uses: number
}

const sameSpot = (a: ImageSpot, b: ImageSpot) => a.line === b.line && a.column === b.column

/** The spots that carry a line, `spot` among them, once each, in document order. */
function knownSpots(spot: ImageSpot, spots: readonly ImageSpot[]): ImageSpot[] {
  const known: ImageSpot[] = []
  for (const s of [...spots, spot]) if (s.line != null && !known.some(k => sameSpot(k, s))) known.push(s)
  return known.sort((a, b) => a.line! - b.line! || (a.column ?? 0) - (b.column ?? 0))
}

/** Which use of its file the image at `spot` is, among every spot the same
 *  `src` is rendered at now. Null when the image has no line. */
export function imageUseAt(spot: ImageSpot, spots: readonly ImageSpot[]): ImageUse | null {
  if (spot.line == null) return null
  const known = knownSpots(spot, spots)
  return { nth: known.findIndex(s => sameSpot(s, spot)), uses: known.length }
}

/**
 * True when a saved box belongs to the image rendered at `spot`, given every
 * spot an image with the same `src` is rendered at now. A text comment finds its
 * passage by its text, not by where it was (`findBestOccurrence`); a box finds
 * its image by `src` the same way. When the file is used more than once, the box
 * stays on the same use (the 2nd of 3) while the number of uses is unchanged, so
 * an edit anywhere else in the document leaves it put. Matching by `src` alone
 * would draw the box on every copy of the image, so a reader who boxed the
 * second screenshot would see the same box on the first. When a use was added or
 * removed it falls back to the use nearest the line and column it was drawn at.
 * Without a line on either side this is `boxMatchesImage`, which matches on what
 * is known.
 */
export function boxBelongsToImage(
  box: { src: string; line?: number; column?: number; nth?: number; uses?: number },
  src: string,
  spot: ImageSpot,
  spots: readonly ImageSpot[],
): boolean {
  if (box.src !== src) return false
  if (box.line == null || spot.line == null) return boxMatchesImage(box, src, spot.line, spot.column)
  const known = knownSpots(spot, spots)
  if (box.nth != null && box.uses === known.length) return sameSpot(known[box.nth], spot)
  let best: ImageSpot | null = null
  let bestDist: [number, number] = [Infinity, Infinity]
  // Nearest line first, then nearest column; `known` is in document order, so
  // a tie stays with the earlier use.
  for (const s of known) {
    const dist: [number, number] = [Math.abs(s.line! - box.line), Math.abs((s.column ?? 0) - (box.column ?? 0))]
    if (dist[0] < bestDist[0] || (dist[0] === bestDist[0] && dist[1] < bestDist[1])) {
      best = s
      bestDist = dist
    }
  }
  return best != null && sameSpot(best, spot)
}

/** The four box fields alone, so a placement's other fields never reach what
 *  the host stores. */
export function boxRectOf(box: ImageBoxRect): ImageBoxRect {
  return { x: box.x, y: box.y, w: box.w, h: box.h }
}

/** What a pending row calls an image: its alt text or file name, cut at a word
 *  past 32 characters. Alt text runs long (agents describe the whole image),
 *  and a long label pushes the box number away from the comment under it. */
export function shortImageLabel(label: string): string {
  if (label.length <= 32) return label
  const cut = label.slice(0, 32)
  const space = cut.lastIndexOf(' ')
  return (space >= 16 ? cut.slice(0, space) : cut).trimEnd() + '…'
}

/** What a pending row calls an image with no alt text: the last segment of its
 *  destination, with any query or fragment dropped, so the row stays short. */
export function fileNameOf(src: string): string {
  const path = src.split(/[?#]/, 1)[0]
  const name = pathBasename(stripTrailingSeparators(path))
  if (!name) return src
  try { return decodeURIComponent(name) } catch { return name }
}

/** The label a box comment is saved with: the image's alt text, trimmed, or
 *  its file name when the alt text is missing or only spaces. Empty for an
 *  inline data image with no alt text, which the row names by the box's spot. */
export function imageAnchorOf(alt: string | undefined, src: string): string {
  return alt?.trim() || fileNameOf(src)
}

/** The one slot a file's box draft is kept in (see `composerDraftStoreFor`). */
export const IMAGE_BOX_DRAFT_SLOT = 'image-box'

/** A box being commented on, as the panel keeps it across a teardown. */
export interface SavedImageBoxDraft extends ImageBoxRect, Partial<ImageUse> {
  src: string
  path?: string
  alt?: string
  line?: number
  column?: number
  text: string
}

type BoxDraftLike = ImageBoxRect & Partial<ImageUse> & { src: string; path?: string; alt?: string; line?: number; column?: number }

/** Identity of one box draft in one file: a new box is a new comment. */
export function imageDraftKeyOf(d: BoxDraftLike & { filePath: string }): string {
  return `${d.filePath}|${d.src}|${d.line}|${d.x}|${d.y}|${d.w}|${d.h}`
}

export function serializeImageBoxDraft(d: BoxDraftLike, text: string): string {
  const saved: SavedImageBoxDraft = {
    src: d.src, x: d.x, y: d.y, w: d.w, h: d.h, text,
    ...(d.path ? { path: d.path } : {}), ...(d.alt ? { alt: d.alt } : {}),
    ...(d.line != null ? { line: d.line } : {}), ...(d.column != null ? { column: d.column } : {}),
    ...(d.nth != null && d.uses != null ? { nth: d.nth, uses: d.uses } : {}),
  }
  return JSON.stringify(saved)
}

const isFraction = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n) && n >= 0 && n <= 1
const isOptInt = (n: unknown) => n === undefined || (typeof n === 'number' && Number.isInteger(n) && n >= 0)
const isOptStr = (s: unknown) => s === undefined || typeof s === 'string'

/** A kept box draft, or null when `raw` is missing or isn't one: a box inside
 *  its image, with text worth keeping. */
export function parseImageBoxDraft(raw: string | null): SavedImageBoxDraft | null {
  if (!raw) return null
  let v: unknown
  try { v = JSON.parse(raw) } catch { return null }
  if (!v || typeof v !== 'object') return null
  const d = v as Record<string, unknown>
  if (typeof d.src !== 'string' || !d.src || typeof d.text !== 'string' || !d.text.trim()) return null
  if (![d.x, d.y, d.w, d.h].every(isFraction) || (d.x as number) + (d.w as number) > 1 || (d.y as number) + (d.h as number) > 1) return null
  if (!isOptStr(d.path) || !isOptStr(d.alt) || !isOptInt(d.line) || !isOptInt(d.column) || !isOptInt(d.nth) || !isOptInt(d.uses)) return null
  // Which use of the file: both or neither, and the use exists, as the
  // pending-comment check in commentDrafts requires.
  if ((d.nth === undefined) !== (d.uses === undefined)) return null
  if (d.nth !== undefined && (d.nth as number) >= (d.uses as number)) return null
  return d as unknown as SavedImageBoxDraft
}
