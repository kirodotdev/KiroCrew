/**
 * His note on a board's paper (D4 "没有comment就还给bot", D18 "就像领导批示工作一样"): words in a hand-written look, in a
 * pen's blue ink, a little turned, in the margin under the Bot's words, as a manager writes on work handed to him.
 * The look is drawn, not a face: the page's own letters, slanted, each a little turned and off the line, the same on
 * every browser (`penLine`), so it needs no font that a system may lack. A word of a script whose letters join (Hindi,
 * Bengali, Arabic) is drawn whole, slanted, and turns and leaves the line as one piece.
 * While the line to write on is open and still empty, a faint ruled line says how to write; closed and empty, a
 * smaller one says where. Drawn on the board's canvas (`boardPicture.ts`), in its px.
 */
import { i18nT } from '../../../i18n/t'
import { css } from '../palette'
import { wrapLines } from '../text'

/** The note's place on the paper and its look, in the 1024 px picture's px. */
export const NOTE = { h: 120, size: 44, lines: 2, ink: 0x1f4fbf, faint: 0x8a93a3, turn: -0.018 } as const

/** The pen's hand: how far the letters lean, how much each turns and leaves the line, and how close they sit. */
export const PEN = { slant: -0.22, turn: 0.05, rise: 2.4, tight: 0.96 } as const

const family = () => (typeof document === 'undefined' ? '' : getComputedStyle(document.body).fontFamily) || 'sans-serif'

/** A repeatable random in [-1, 1) for the `i`th letter: the same note looks the same at every redraw. */
const wobble = (i: number, k: number) => { const x = Math.sin(i * 12.9898 + k * 78.233) * 43758.5453; return (x - Math.floor(x)) * 2 - 1 }

/**
 * Scripts whose letters join or stack into one shape (Hebrew and Arabic to N'Ko, the Indic scripts and Sinhala, Thai,
 * Lao, Tibetan, Myanmar, Khmer to Mongolian, and the Arabic and Hebrew presentation forms).
 */
const JOINED = /[\u0590-\u08FF\u0900-\u0DFF\u0E00-\u0EFF\u0F00-\u0FFF\u1000-\u109F\u1780-\u18AF\uA8E0-\uA8FF\uFB1D-\uFDFF\uFE70-\uFEFF]/

const segmenter = typeof Intl !== 'undefined' && 'Segmenter' in Intl ? new Intl.Segmenter(undefined, { granularity: 'grapheme' }) : null
/** The letters of `text` as the eye reads them; by code point where the browser cannot tell. */
const graphemes = (text: string): string[] => (segmenter ? [...segmenter.segment(text)].map(s => s.segment) : [...text])

/**
 * The pieces a line is drawn in: each letter on its own (Latin, Chinese and the rest, as a pen leaves them), but a word
 * of a script whose letters join (`JOINED`) whole, so the browser shapes it as one. `i` is the wobble's index: the
 * place of the piece's first letter in the line, so a word wobbles as one.
 */
export function penPieces(line: string): { text: string; i: number }[] {
  const out: { text: string; i: number }[] = []
  let i = 0
  for (const run of line.split(/(\s+)/).filter(Boolean)) {
    // A letter is what the eye reads as one (a grapheme): an emoji with its skin tone, a letter with its accent mark.
    const letters = graphemes(run)
    if (JOINED.test(run) && !/\s/.test(run)) out.push({ text: run, i })
    else letters.forEach((ch, k) => out.push({ text: ch, i: i + k }))
    i += letters.length
  }
  // shortcut: words are placed left to right, so a right-to-left line (Arabic, Hebrew) shows its words in reverse
  // order; upgrade to the browser's own bidi layout when the dashboard ships a right-to-left language.
  return out
}

/**
 * One line in his hand at (`x`, `y`), piece by piece (`penPieces`): leaning (`PEN.slant`), each piece turned a little
 * and a little off the line, as a pen leaves them. Returns how wide it was drawn. The pieces are pressed closer when
 * the line would pass `max`.
 */
export function penLine(c: CanvasRenderingContext2D, line: string, x: number, y: number, max: number): number {
  const pieces = penPieces(line)
  const natural = pieces.reduce((w, p) => w + c.measureText(p.text).width, 0) * PEN.tight
  const squeeze = natural > max ? max / natural : 1
  let at = 0
  for (const p of pieces) {
    const w = c.measureText(p.text).width * PEN.tight * squeeze
    c.save()
    c.translate(x + at, y + wobble(p.i, 1) * PEN.rise)
    c.transform(1, 0, PEN.slant, 1, 0, 0)
    c.rotate(wobble(p.i, 2) * PEN.turn)
    // A whole word takes the line's squeeze inside itself; a letter is only placed closer.
    if (squeeze < 1 && p.text.length > 1) c.scale(squeeze, 1)
    c.fillText(p.text, 0, 0)
    c.restore()
    at += w
  }
  return at
}

/** The pen's font: the page's own letters, at `size` px. */
export const penFont = (size: number) => `500 ${size}px ${family()}`

/** What the board's note shows: its words, the words still heard, and whether its line is open and listening. */
export interface NoteDraw { text: string; interim: string; writing: boolean; listening: boolean }

type Box = { x: number; y: number; w: number; h: number }

/** What a note shows: its words, then the words still heard. */
export const noteWords = (note: Pick<NoteDraw, 'text' | 'interim'>): string => `${note.text}${note.interim ? `${note.text ? ' ' : ''}${note.interim}` : ''}`.trim()

/** Draw the note into `box` (the strip at the paper's foot that a pinch opens). */
export function drawNote(c: CanvasRenderingContext2D, box: Box, note: NoteDraw): void {
  const words = noteWords(note)
  c.save()
  c.textBaseline = 'top'
  c.textAlign = 'left'
  if (!words) {
    // The ruled line to write on, and one small line of print.
    c.strokeStyle = css(NOTE.faint)
    c.lineWidth = 2
    c.setLineDash(note.writing ? [] : [10, 10])
    c.beginPath(); c.moveTo(box.x + 12, box.y + box.h - 18); c.lineTo(box.x + box.w - 12, box.y + box.h - 18); c.stroke()
    c.setLineDash([])
    c.fillStyle = css(NOTE.faint)
    c.font = penFont(note.writing ? 32 : 26)
    c.fillText(i18nT(note.writing ? (note.listening ? 'xr.note_listening' : 'xr.note_type') : 'xr.note_line'), box.x + 12, box.y + box.h - 62, box.w - 24)
    c.restore()
    return
  }
  c.translate(box.x + 12, box.y + 8)
  c.rotate(NOTE.turn)
  c.font = penFont(NOTE.size)
  const lines = wrapLines(s => c.measureText(s).width * PEN.tight, words, box.w - 36, NOTE.lines).lines
  c.fillStyle = css(NOTE.ink)
  let end = 0
  lines.forEach((line, i) => { end = penLine(c, line, 0, i * NOTE.size * 1.15, box.w - 36) })
  if (note.writing) {
    // The pen's place: a short stroke after the last word, while the line is open.
    const x = Math.min(box.w - 40, end + 8)
    c.fillStyle = css(NOTE.ink)
    c.fillRect(x, (lines.length - 1) * NOTE.size * 1.15 + 4, 3, NOTE.size)
  }
  c.restore()
}
