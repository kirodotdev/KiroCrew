/**
 * The picture of the palette's tiles: flat, dark, with the panel's own border, the dashboard's own icon over the
 * dashboard's own label. One canvas for all the tiles; the palette draws it on one rectangle (`Palette.tsx`).
 *
 * The icons are the ones the dashboard draws: the workspace renders them once, out of sight, and this reads them
 * from there. An icon is a line drawing (`lucide-react`, an app's inlined mark) or a mark that the dashboard cuts
 * out of a colour with a mask. Either way it is drawn here in one colour, the panels' own ink: on glasses that add
 * light, the theme's colours are not what a person sees.
 */
import { XR_GL, css } from '../palette'
import { PALM_MENU, paletteSize, tiles } from './palm'

/** Canvas pixels for each dp: enough for the glasses' display at arm's length, with room for a slanted look. */
export const PX_PER_DP = 3

/** The dashboard's icon inside `holder`, as a picture `px` wide and high, or null when there is none to read yet. */
export async function iconPicture(holder: Element | null, px: number): Promise<HTMLImageElement | null> {
  if (!holder) return null
  let src = ''
  const svg = holder.querySelector('svg')
  if (svg) {
    const copy = svg.cloneNode(true) as SVGElement
    copy.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
    copy.setAttribute('width', String(px))
    copy.setAttribute('height', String(px))
    src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(copy))}`
  } else {
    // A mark that is cut out of a colour: the mask's own picture is the mark.
    for (const el of holder.querySelectorAll<HTMLElement>('*')) {
      const mask = el.style.maskImage || el.style.webkitMaskImage
      const url = /^url\((["']?)(.+)\1\)$/.exec(mask ?? '')?.[2]
      if (url) { src = url; break }
    }
  }
  if (!src) return null
  const img = new Image()
  img.src = src
  try { await img.decode() } catch { return null }
  return img
}

/** Draw the tiles: `labels[i]` under `icons[i]`. Returns the canvas's size in pixels. */
export function drawPalette(canvas: HTMLCanvasElement, labels: readonly string[], icons: readonly (CanvasImageSource | null)[]): [number, number] {
  const [wDp, hDp] = paletteSize(labels.length)
  const k = PX_PER_DP
  canvas.width = wDp * k
  canvas.height = hDp * k
  const c = canvas.getContext('2d')
  if (!c) return [canvas.width, canvas.height]
  const ink = css(XR_GL.ink)
  const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
  const iconPx = PALM_MENU.iconDp * k
  // One icon at a time is tinted on a scratch canvas: the picture keeps its shape, and takes the ink's colour.
  const scratch = document.createElement('canvas')
  scratch.width = scratch.height = iconPx
  const s = scratch.getContext('2d')
  c.clearRect(0, 0, canvas.width, canvas.height)
  c.textAlign = 'center'
  c.textBaseline = 'middle'
  tiles(labels.length).forEach((tile, i) => {
    const w = tile.w * k
    const h = tile.h * k
    const left = (tile.x + wDp / 2) * k - w / 2
    const top = (hDp / 2 - tile.y) * k - h / 2
    const line = 1.5 * k
    c.fillStyle = css(XR_GL.faint)
    c.fillRect(left, top, w, h)
    c.fillStyle = css(XR_GL.raised)
    c.fillRect(left + line, top + line, w - 2 * line, h - 2 * line)
    const icon = icons[i]
    if (icon && s) {
      s.globalCompositeOperation = 'source-over'
      s.clearRect(0, 0, iconPx, iconPx)
      s.drawImage(icon, 0, 0, iconPx, iconPx)
      s.globalCompositeOperation = 'source-in'
      s.fillStyle = ink
      s.fillRect(0, 0, iconPx, iconPx)
      c.drawImage(scratch, left + (w - iconPx) / 2, top + 12 * k)
    }
    // The label at 14 dp. A longer word of another language is drawn smaller, down to 11 dp, and then cut.
    let size = PALM_MENU.labelDp * k
    let text = labels[i]
    const room = w - 12 * k
    c.font = `600 ${size}px ${family}`
    while (size > 11 * k && c.measureText(text).width > room) { size -= k / 2; c.font = `600 ${size}px ${family}` }
    while (text.length > 2 && c.measureText(text).width > room) text = `${text.slice(0, -2)}…`
    c.fillStyle = ink
    c.fillText(text, left + w / 2, top + h - (PALM_MENU.labelDp / 2 + 10) * k)
  })
  return [canvas.width, canvas.height]
}
