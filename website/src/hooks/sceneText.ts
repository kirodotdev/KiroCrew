/**
 * Shared text rendering for Worlds scenes.
 * Provides a high-DPI text overlay canvas and auto-sized text labels.
 */

import type { CSSProperties } from 'react'
import { SCENE_SCALE, SCENE_LAYOUT_SCALE } from '../pages/scenes/config'

const FONT_FAMILY = '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif'
const TEXT_SCALE = SCENE_SCALE / SCENE_LAYOUT_SCALE
const BASE_SIZE = 14 * TEXT_SCALE

type TextRole = 'title' | 'name' | 'status' | 'detail' | 'label' | 'spell'
const ROLE_SCALE: Record<TextRole, number> = {
  title: 1.1,
  spell: 0.85,
  name: 0.8,
  status: 0.66,
  detail: 0.58,
  label: 0.5,
}

/** Get a font string for a given role and optional weight */
export function sceneFont(role: TextRole, weight: '' | 'bold' = ''): string {
  const size = BASE_SIZE * ROLE_SCALE[role]
  return weight ? `${weight} ${size}px ${FONT_FAMILY}` : `${size}px ${FONT_FAMILY}`
}

/** Get the computed font size for a role */
export function sceneFontSize(role: TextRole): number {
  return BASE_SIZE * ROLE_SCALE[role]
}

/** Vertical advance between wrapped lines for a role, in canvas px */
export function sceneLineHeight(role: TextRole): number {
  return sceneFontSize(role) * 1.3
}

/** Word-wrap text to fit maxWidth using the current canvas font. Hard-breaks over-long words. */
function wrapText(T: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  if (T.measureText(text).width <= maxWidth) return [text]
  const words = text.split(/\s+/).filter(Boolean)
  const lines: string[] = []
  let line = ''
  const push = () => { if (line) { lines.push(line); line = '' } }
  for (const word of words) {
    const candidate = line ? line + ' ' + word : word
    if (T.measureText(candidate).width <= maxWidth) {
      line = candidate
      continue
    }
    push()
    if (T.measureText(word).width <= maxWidth) {
      line = word
      continue
    }
    // Hard-break a single word longer than the line
    let chunk = ''
    for (const ch of word) {
      if (T.measureText(chunk + ch).width > maxWidth && chunk) {
        lines.push(chunk)
        chunk = ch
      } else {
        chunk += ch
      }
    }
    line = chunk
  }
  push()
  return lines.length ? lines : [text]
}

/**
 * Initialize a text overlay canvas for high-DPI rendering.
 * Returns the 2D context. Call once during effect setup.
 */
export function initTextCanvas(canvas: HTMLCanvasElement, w: number, h: number, scale: number): CanvasRenderingContext2D {
  const dpr = window.devicePixelRatio || 1
  const cssW = w * scale
  const cssH = h * scale
  canvas.width = cssW * dpr
  canvas.height = cssH * dpr
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('Failed to get 2d context for text canvas')
  ctx.imageSmoothingEnabled = true
  ctx.scale(dpr, dpr)
  return ctx
}

/**
 * Draw text with an optional auto-sized background box.
 * Both background and text are drawn on the text canvas (T) for correct DPI alignment.
 * When `maxWidth` (canvas px) is provided, text word-wraps into multiple lines.
 * Returns the number of lines drawn so callers can offset content below.
 */
export function drawLabel(
  T: CanvasRenderingContext2D,
  text: string,
  x: number, y: number,
  opts: {
    role: TextRole
    weight?: '' | 'bold'
    color: string
    bgColor?: string
    align?: CanvasTextAlign
    padX?: number
    padY?: number
    scale: number
    maxWidth?: number
  },
): number {
  const { role, weight = '', color, bgColor, align = 'start', padX = 3, padY = 2, scale: S, maxWidth } = opts
  T.font = sceneFont(role, weight)
  T.fillStyle = color
  T.textAlign = align
  T.textBaseline = 'middle'

  const lines = maxWidth ? wrapText(T, text, maxWidth) : [text]
  const lineH = sceneLineHeight(role)

  lines.forEach((line, i) => {
    const ly = y + i * lineH
    if (bgColor) {
      const tw = T.measureText(line).width
      const fontSize = sceneFontSize(role)
      const bh = fontSize + padY * 2 * S
      let bx = x
      if (align === 'center') bx = x - tw / 2
      else if (align === 'end') bx = x - tw
      T.fillStyle = bgColor
      T.fillRect(bx - padX * S, ly - bh / 2, tw + padX * 2 * S, bh)
      T.fillStyle = color
    }
    T.fillText(line, x, ly)
  })

  T.textAlign = 'start'
  T.textBaseline = 'alphabetic'
  return lines.length
}

/** How long a fresh message bubble stays visible, in ms */
export const SPEECH_BUBBLE_MS = 7000

/** Kiro ghost bitmap, 24×28 — traced from the reference art. Shared by the
 *  Ghost scene sprite and the mini agent avatar in the thread popover. */
export const KIRO_GHOST_PIXELS = [
  '..........#######.......',
  '.......#############....',
  '......##############....',
  '......###############...',
  '.....#################..',
  '....##################..',
  '....###################.',
  '....###################.',
  '....####################',
  '...#####################',
  '...#####################',
  '...#####################',
  '...#####################',
  '...#####################',
  '..######################',
  '..######################',
  '.#######################',
  '.######################.',
  '#######################.',
  '######################..',
  '.#####################..',
  '.###.#################..',
  '....#################...',
  '....#################...',
  '....########.#######....',
  '....#######..######.....',
  '....#######...####......',
  '......###...............',
]

/**
 * Draw a speech bubble with wrapped text above an agent.
 * (bx, by) is the bubble tail anchor in canvas px (the agent's head).
 * Text is clamped to `maxLines` with an ellipsis on the last line.
 */
export function drawSpeechBubble(
  T: CanvasRenderingContext2D,
  text: string,
  bx: number, by: number,
  opts: { scale: number; maxWidth?: number; maxLines?: number; alpha?: number },
) {
  const { scale: S, maxWidth = 82 * S, maxLines = 3, alpha = 1 } = opts
  const role = 'detail'
  T.font = sceneFont(role)
  const flat = text.replace(/\s+/g, ' ').trim()
  if (!flat) return
  let lines = wrapText(T, flat, maxWidth)
  if (lines.length > maxLines) {
    lines = lines.slice(0, maxLines)
    lines[maxLines - 1] = lines[maxLines - 1].replace(/.{2}$/, '') + '…'
  }
  const lineH = sceneLineHeight(role)
  const padX = 4 * S, padY = 3 * S
  const widest = Math.max(...lines.map(l => T.measureText(l).width))
  const bw = widest + padX * 2
  const bh = lines.length * lineH + padY * 2
  const left = bx - bw / 2
  const top = by - bh - 6 * S

  T.save()
  T.globalAlpha = alpha
  // Bubble body
  T.fillStyle = 'rgba(255,255,255,0.94)'
  T.beginPath()
  const r = 4 * S
  T.roundRect(left, top, bw, bh, r)
  T.fill()
  // Tail
  T.beginPath()
  T.moveTo(bx - 3 * S, top + bh)
  T.lineTo(bx, top + bh + 5 * S)
  T.lineTo(bx + 3 * S, top + bh)
  T.closePath()
  T.fill()
  // Text
  T.fillStyle = '#222'
  T.textAlign = 'left'
  T.textBaseline = 'middle'
  lines.forEach((line, i) => {
    T.fillText(line, left + padX, top + padY + lineH * i + lineH / 2)
  })
  T.restore()
  T.textAlign = 'start'
  T.textBaseline = 'alphabetic'
}

/** CSS props for the text overlay canvas element */
export const TEXT_CANVAS_STYLE: CSSProperties = {
  position: 'absolute',
  inset: 0,
  width: '100%',
  height: '100%',
  pointerEvents: 'none',
  borderRadius: 8,
}

/** CSS props for the scene container div */
export const SCENE_CONTAINER_STYLE = (w: number, h: number): CSSProperties => ({
  position: 'relative',
  maxWidth: '100%',
  aspectRatio: `${w}/${h}`,
})

/** CSS props for the pixel art canvas */
export const PIXEL_CANVAS_STYLE: CSSProperties = {
  imageRendering: 'pixelated',
  border: '2px solid var(--accent, #f90)',
  cursor: 'pointer',
  width: '100%',
  maxHeight: '100%',
  objectFit: 'contain',
  borderRadius: 8,
}
