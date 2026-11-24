/**
 * The picture of a board (Ray's word: a signature clipboard, "like a manager signing off work"): a stiff backing
 * board with rounded corners, a black metal clip at its top middle, and a sheet of white paper held under the clip
 * with a margin of board round it. The words are on the paper, as ink: the sentence large, the command whole in a mono
 * face. A green APPROVED print shows where the stamp was pressed, a little turned and not quite even, and stays.
 *
 * Three kinds: held up by a Bot of the row (the sentence large, the command small), held up at a desk seat (the Bot's
 * name), and in his hand (the sentence, the command always whole, and a small line at the paper's foot that opens the
 * Bot's page): the paper grows with the command up to a cap, past it the command's type gets smaller, and past that
 * the command scrolls in a window on the paper, so every press of the stamp on the paper is the yes. Everything that the room presses on the picture is given back in the picture's own fractions
 * (`u` across, `v` down), so the stamp and the marks need no second measure.
 */
import { useFrame } from '@react-three/fiber'
import { useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { CanvasTexture, LinearFilter, SRGBColorSpace } from 'three'
import { i18nT } from '../../../i18n/t'
import { css, readPalette } from '../palette'
import { crewText, wrapLines } from '../text'
import { NOTE, PEN, drawNote, noteWords, penFont, penLine, type NoteDraw } from './notePicture'
import type { QuestionSheet } from './question'
import { deskWords } from './deskFit'
import { SAYS, type DoneFoot, type Sheet } from './says'

/** The clipboard's own colours: a dark blue backing as in Ray's photo, its black clip, the paper and the inks. */
export const CLIPBOARD = {
  board: 0x34507e,
  boardEdge: 0x4a6aa0,
  clip: 0x1c1f25,
  clipLight: 0x5a606c,
  paper: 0xf7f5ef,
  ink: 0x1d2330,
  faint: 0x6b7280,
  approved: 0x1f9a4f,
  /** A failed sheet's edge and its desk line (P3 slice 2). */
  failed: 0xcf222e,
  /** A linked change's line on a done sheet, by its state (P3): open, merged, closed. */
  open: 0x1a7f37,
  merged: 0x8250df,
  closed: 0xcf222e,
  /** A card tag on the clip: the asks behind the board (D24). */
  tag: 0xe8d6a8,
  /** The back of a sheet's turned corner. */
  fold: 0xe2ded2,
} as const

/** Which board is drawn: held up by a Bot of the row, held up at a seat, or in his hand (or on the floor). */
export type BoardKind = 'bot' | 'seat' | 'held'

/** A place on the picture, in its fractions: `u` across from the left, `v` down from the top. */
export interface Uv { u: number; v: number }
/** A box on the picture, in its fractions. */
export interface UvBox { u0: number; v0: number; u1: number; v1: number }
/** Where a print was pressed, and how much it is turned (radians). */
export interface Print extends Uv { tilt: number; seed: number }

interface Draw {
  kind: BoardKind
  sentence: string
  command: string
  name: string
  print: Print | null
  /** The asks of its Bot that wait behind it: a tag on the clip (D24). */
  behind?: number
  /** In his hand, a sheet of its Bot's words (`says.ts`) in place of an ask; `turns`: the corner turns to another. */
  sheet?: { at: Sheet; turns: boolean; picked?: readonly string[]; prints?: readonly { label: string; print: Print }[] } | null
  /** In his hand, a sheet of its Bot's question: the question, its boxes, the ones marked and their prints. */
  question?: { at: QuestionSheet; picked: readonly string[]; prints: readonly { label: string; print: Print }[]; turns: boolean } | null
  /** In his hand, his note on the paper (`notePicture.ts`). */
  note?: NoteDraw | null
  /** The blank sheet a Bot holds up while he holds a pinch on it (P2): his words on it are `note`. */
  speak?: { listening: boolean } | null
  /** At its Bot, the done sheet's head and lines (P3); in his hand they are on the sheet (`Sheet.done`). */
  done?: DoneFoot | null
  /** A words board at a desk far off (`Board.tsx`, `AT_DESK`): ONE large line, a done sheet's in the done colour. */
  desk?: { line: string; name?: string; done: boolean; failed?: boolean } | null
}

/** In the picture's px: its width, the board's margin round the paper, the clip, the paper's own padding. */
const PX = { width: 1024, margin: 40, clipW: 300, clipH: 92, paperTop: 64, pad: 44 } as const
/** The clip's tag, in the held picture's px: the card, its word's size, and how much larger it is drawn at a Bot. */
const TAG = { w: 168, h: 100, word: 64, atBot: 2 } as const
/** All that the tag draws (string, card, shadow), from where its string starts. */
const TAG_PX = { w: TAG.w + 18, h: TAG.h + 8 } as const
/**
 * The tag at a Bot on its board, in shares of the board's width (the picture is `PX.width` across): its size, and its
 * middle from the board's top middle (`x` right, `y` up). Its string starts under the clip's right end; the card
 * stands `TAG_AT_BOT.overTop` above the board's top edge, and its foot stays above the sentence's first line.
 */
const tagShare = (px: number) => (px * TAG.atBot) / PX.width
const TAG_OVER_TOP = 92 / PX.width
export const TAG_AT_BOT = {
  w: tagShare(TAG_PX.w),
  h: tagShare(TAG_PX.h),
  x: (PX.clipW / 2 - 14 * TAG.atBot) / PX.width + tagShare(TAG_PX.w) / 2,
  y: TAG_OVER_TOP - tagShare(TAG_PX.h) / 2,
  overTop: TAG_OVER_TOP,
  turn: 0.1,
} as const
/** The words, by kind: the sentence's size and its most lines, the command's size. */
const WORDS = { held: { sentence: 50, lines: 3, command: 32 }, bot: { sentence: 76, lines: 2, command: 30 } } as const
/**
 * In his hand the paper grows with the command until the picture is this tall to its width (0.45 m on the held
 * board); past it the command's type gets smaller, down to `commandMin` px, and past that the command is a window on
 * the paper that scrolls, so the picture never grows past the cap.
 */
const HELD_CAP = { aspect: 0.9, commandMin: 20 } as const
/** The print, in the held picture's px: its box and its word's size; it is turned between these (radians, to the left). */
const PRINT = { w: 280, h: 96, word: 54, tiltMin: 0.08, tiltMax: 0.22 } as const
/** The stamp's rubber face is the print: its width as a share of the picture's, and its height to its width. */
export const PRINT_FACE = { u: PRINT.w / PX.width, ratio: PRINT.h / PRINT.w } as const
/** A print's turn, from a random in [0, 1). */
export const printTilt = (r: number) => -(PRINT.tiltMin + r * (PRINT.tiltMax - PRINT.tiltMin))

const FAMILY = () => getComputedStyle(document.body).fontFamily || 'sans-serif'
/** The theme's own monospace stack (`--mono`). */
const monoFamily = () => getComputedStyle(document.body).getPropertyValue('--mono').trim() || 'monospace'

/** A random of its own seed, so a print looks the same at every redraw. */
function seeded(seed: number) {
  let s = seed >>> 0 || 1
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296 }
}

/** The backing board and its clip, over the whole picture. */
function drawBoard(c: CanvasRenderingContext2D, w: number, h: number, small: boolean) {
  const r = small ? 22 : 34
  c.fillStyle = css(CLIPBOARD.board)
  c.beginPath(); c.roundRect(0, 0, w, h, r); c.fill()
  // A lighter rim, so the board reads against the dark room.
  c.strokeStyle = css(CLIPBOARD.boardEdge)
  c.lineWidth = small ? 4 : 6
  c.beginPath(); c.roundRect(3, 3, w - 6, h - 6, r - 2); c.stroke()
}

/** The metal clip at the top middle, drawn over the paper's top edge. */
function drawClip(c: CanvasRenderingContext2D, w: number, scale: number, behind = 0) {
  const cw = PX.clipW * scale
  const ch = PX.clipH * scale
  const x = (w - cw) / 2
  const y = 10 * scale
  if (behind > 0) drawTag(c, x + cw - 14 * scale, y + 2 * scale, scale, behind)
  c.fillStyle = css(CLIPBOARD.clip)
  c.beginPath(); c.roundRect(x, y, cw, ch, 14 * scale); c.fill()
  // Its lever, and a highlight along its top, as on a metal clip.
  c.fillStyle = css(CLIPBOARD.clipLight)
  c.beginPath(); c.roundRect(x + cw * 0.18, y + 12 * scale, cw * 0.64, 10 * scale, 5 * scale); c.fill()
  c.strokeStyle = css(CLIPBOARD.clip)
  c.lineWidth = 9 * scale
  c.beginPath(); c.arc(w / 2, y + ch * 0.2, cw * 0.18, Math.PI * 1.05, Math.PI * 1.95); c.stroke()
}

/**
 * The asks that wait behind the board (D24): a small card tag hung on the metal clip by a loop of string, a little
 * turned (`turn`), with "+1" on it in the paper's ink. It is drawn under the clip, so the clip holds it.
 */
function drawTag(c: CanvasRenderingContext2D, at: number, top: number, scale: number, behind: number, turn = -0.1) {
  const tw = TAG.w * scale
  const th = TAG.h * scale
  c.save()
  c.translate(at, top)
  c.rotate(turn)
  // The string's loop, from the clip to the tag's hole.
  c.strokeStyle = css(CLIPBOARD.faint)
  c.lineWidth = 3 * scale
  c.beginPath(); c.moveTo(4 * scale, 0); c.quadraticCurveTo(16 * scale, 12 * scale, 30 * scale, th * 0.5); c.stroke()
  // The tag: a card with its two near corners cut, and a ringed hole.
  c.fillStyle = 'rgba(0, 0, 0, 0.3)'
  c.beginPath(); c.roundRect(14 * scale, 5 * scale, tw, th, 8 * scale); c.fill()
  c.fillStyle = css(CLIPBOARD.tag)
  c.beginPath()
  c.moveTo(10 * scale + 22 * scale, 0)
  c.lineTo(10 * scale + tw, 0); c.lineTo(10 * scale + tw, th); c.lineTo(10 * scale + 22 * scale, th)
  c.lineTo(10 * scale, th - 22 * scale); c.lineTo(10 * scale, 22 * scale)
  c.closePath(); c.fill()
  c.fillStyle = css(CLIPBOARD.clip)
  c.beginPath(); c.arc(30 * scale, th * 0.5, 8 * scale, 0, Math.PI * 2); c.fill()
  c.fillStyle = css(CLIPBOARD.ink)
  c.font = `800 ${TAG.word * scale}px ${FAMILY()}`
  c.textAlign = 'center'
  c.textBaseline = 'middle'
  c.fillText(i18nT('xr.board_behind', { count: behind }), 10 * scale + (tw + 40 * scale) / 2, th * 0.54)
  c.restore()
}

/**
 * The tag at a Bot, as its own picture: there it is the one thing that says more waits, so it is drawn `TAG.atBot`
 * times its size in his hand, its "+1" taller than the board's own sentence. It hangs beside the clip and stands out
 * past the board's top edge, so it covers none of the words. Its place and size on the board are `TAG_AT_BOT`, in
 * shares of the board's width; `null` when nothing waits.
 */
export function useTagPicture(behind: number): CanvasTexture | null {
  const texture = useMemo(() => {
    if (behind <= 0) return null
    const s = TAG.atBot
    const canvas = document.createElement('canvas')
    canvas.width = Math.ceil(TAG_PX.w * s)
    canvas.height = Math.ceil(TAG_PX.h * s)
    const c = canvas.getContext('2d')
    if (c) drawTag(c, 0, 0, s, behind, 0)
    const t = new CanvasTexture(canvas)
    t.colorSpace = SRGBColorSpace
    t.minFilter = LinearFilter
    t.generateMipmaps = false
    return t
  }, [behind])
  useEffect(() => () => texture?.dispose(), [texture])
  return texture
}

/** The sheet of paper and its faint shadow on the board. */
function drawPaper(c: CanvasRenderingContext2D, box: { x: number; y: number; w: number; h: number }) {
  c.fillStyle = 'rgba(0, 0, 0, 0.28)'
  c.fillRect(box.x + 5, box.y + 7, box.w, box.h)
  c.fillStyle = css(CLIPBOARD.paper)
  c.fillRect(box.x, box.y, box.w, box.h)
}

/**
 * The word a print carries: the green "APPROVED" of Ray's D17, on an approval and on a question's or a choice's box
 * (D44); only the failed sheet's box prints its own short word, "TRY AGAIN" (D42).
 */
export function printWord(on: 'approval' | 'retry' | 'box'): string {
  return on === 'retry' ? i18nT('xr.fail_try_print') : i18nT('xr.board_print')
}

/**
 * A green print at `at` (picture px), turned by `tilt`: a frame and its word (`printWord`), in
 * ink that took unevenly, with a few specks where the rubber did not touch.
 */
function drawPrint(c: CanvasRenderingContext2D, at: { x: number; y: number }, tilt: number, seed: number, scale: number, word: string) {
  const rnd = seeded(seed)
  const w = PRINT.w * scale
  const h = PRINT.h * scale
  c.save()
  c.translate(at.x, at.y)
  c.rotate(tilt)
  const ink = css(CLIPBOARD.approved)
  // Ink takes unevenly: the frame and the word are laid three times, each a little off and a little lighter.
  for (let k = 0; k < 3; k += 1) {
    c.globalAlpha = [0.78, 0.35, 0.22][k]
    const dx = (rnd() - 0.5) * 3 * scale
    const dy = (rnd() - 0.5) * 3 * scale
    c.strokeStyle = ink
    c.lineWidth = 9 * scale
    c.beginPath(); c.roundRect(-w / 2 + dx, -h / 2 + dy, w, h, 16 * scale); c.stroke()
    c.fillStyle = ink
    c.font = `900 ${PRINT.word * scale}px ${FAMILY()}`
    c.textAlign = 'center'
    c.textBaseline = 'middle'
    c.fillText(word, dx, dy + 3 * scale, w - 28 * scale)
  }
  // Where the rubber did not touch: small specks of paper in the ink.
  c.globalAlpha = 0.85
  c.fillStyle = css(CLIPBOARD.paper)
  for (let k = 0; k < 140; k += 1) {
    c.beginPath()
    c.arc((rnd() - 0.5) * w, (rnd() - 0.5) * h, (0.6 + rnd() * 2.4) * scale, 0, Math.PI * 2)
    c.fill()
  }
  c.restore()
}

/** What the picture gives back: its texture, its height to width, and the boxes the room presses, in fractions. */
export interface BoardPic {
  texture: CanvasTexture
  aspect: number
  paper: UvBox
  pageLine: UvBox | null
  /** On the last sheet of a Bot's words that left something out: the mark that opens the Bot's page. */
  pageMark: UvBox | null
  /** The command's window when it is longer than the paper holds: where it is, its last first row, a row's height. */
  scroll: { box: UvBox; maxRow: number; rowV: number } | null
  /** A question sheet's boxes, in order; none on another board. */
  boxes: UvBox[]
  /** In his hand: the strip at the paper's foot where his note is written (a pinch there opens its line). */
  noteLine: UvBox | null
  /** A board at a desk: its one line's letter size, as a share of the picture's width; 0 on any other. */
  deskLetter: number
  /** The desk line as it is drawn, with its ellipsis when it was cut. */
  deskDrawn: string
}

type Px = { x: number; y: number; w: number; h: number }
/** A board's picture laid out once for what it shows; `paint` draws it with the command's window at a first row. */
interface Laid { w: number; h: number; paper: Px; pageLine: Px | null; mark?: Px | null; boxes?: Px[]; note?: Px | null; scroll: { box: Px; maxRow: number; rowPx: number } | null; paint: (c: CanvasRenderingContext2D, row: number) => void; letter?: number; drawnLine?: string }
/** What a picture's frame takes besides its height and its inner drawing. */
type FrameMore = { scroll?: Laid['scroll']; pageLine?: Px | null; mark?: Px | null; boxes?: Px[]; note?: Px | null }
type Frame = (h: number, inner: (c2: CanvasRenderingContext2D, paper: Px, row: number) => void, more?: FrameMore) => Laid

/** Commands wrapped lately, by their text, font and width: a long command is wrapped once, not at every scroll. */
const wrapped = new Map<string, string[]>()
function wrapOnce(c: CanvasRenderingContext2D, text: string, max: number, font: string, maxLines: number): string[] {
  const key = `${font}|${max}|${maxLines}|${text}`
  const got = wrapped.get(key)
  if (got) return got
  c.font = font
  const lines = wrapLines(s => c.measureText(s).width, text, max, maxLines).lines
  // shortcut: the last 24 layouts are kept, enough for the boards in the room at once.
  if (wrapped.size >= 24) wrapped.delete(wrapped.keys().next().value as string)
  wrapped.set(key, lines)
  return lines
}

/** Lay a board's picture out: its size, its boxes, and how to paint it. */
export function layBoard(d: Draw, c: CanvasRenderingContext2D): Laid {
  const family = FAMILY()
  const small = d.kind !== 'held'
  const w = d.kind === 'seat' ? 512 : PX.width
  const scale = w / PX.width
  const margin = PX.margin * scale
  const paperTop = (d.kind === 'seat' ? 46 : PX.paperTop) * scale
  const pad = (d.kind === 'seat' ? 22 : PX.pad) * scale
  const textTop = paperTop + (d.kind === 'seat' ? 30 : 64) * scale
  const max = w - 2 * margin - 2 * pad
  const frame: Frame = (h, inner, more = {}) => {
    const paper = { x: margin, y: paperTop, w: w - 2 * margin, h: h - paperTop - margin }
    return {
      w, h, paper, pageLine: more.pageLine ?? null, mark: more.mark ?? null, boxes: more.boxes, note: more.note ?? null, scroll: more.scroll ?? null,
      paint: (c2, row) => {
        c2.clearRect(0, 0, w, h)
        drawBoard(c2, w, h, small)
        drawPaper(c2, paper)
        inner(c2, paper, row)
        if (d.note && more.note) drawNote(c2, more.note, d.note)
        if (d.print && d.kind === 'held') drawPrint(c2, { x: d.print.u * w, y: d.print.v * h }, d.print.tilt, d.print.seed, scale, printWord('approval'))
        // At a Bot the tag is a picture of its own (`useTagPicture`), larger and past the board's edge.
        drawClip(c2, w, d.kind === 'seat' ? scale * 0.9 : scale, d.kind === 'held' ? d.behind ?? 0 : 0)
      },
    }
  }
  if (d.kind === 'seat') {
    // The name as large as fits the paper.
    let size = 96
    c.font = `800 ${size}px ${family}`
    const wide = c.measureText(d.name).width
    if (wide > max) size = Math.max(36, Math.floor((size * max) / wide))
    return frame(Math.ceil(textTop + size * 1.15 + pad + margin), c2 => { c2.font = `800 ${size}px ${family}`; c2.fillStyle = css(CLIPBOARD.ink); c2.textBaseline = 'top'; c2.textAlign = 'center'; c2.fillText(d.name, w / 2, textTop, max) })
  }
  const held = d.kind === 'held'
  if (d.speak && d.note) return laySpeak(d.name, d.note, d.speak.listening, { textTop, pad, margin, max, family }, frame)
  if (held && d.sheet) return laySheet(d.sheet, d.name, { textTop, pad, margin, max, family }, frame)
  if (held && d.question) return layQuestion(d.question, d.name, c, { textTop, pad, margin, max, family }, frame)
  if (d.kind === 'bot' && d.desk) return layDesk(d.desk, c, { pad, margin, max, family, paperTop }, frame)
  if (d.kind === 'bot' && d.done) return layDoneAtBot(d.done, wrapOnce(c, crewText(d.sentence), max, `700 ${WORDS.bot.sentence}px ${family}`, WORDS.bot.lines), { textTop, pad, max, family, margin }, frame)
  const words = held ? WORDS.held : WORDS.bot
  const lines = wrapOnce(c, crewText(d.sentence), max, `700 ${words.sentence}px ${family}`, words.lines)
  const mono = monoFamily()
  // In his hand the paper's foot holds the line under a scrolled command and his note's strip.
  const foot = held ? 60 + NOTE.h : 0
  const commandTop = textTop + lines.length * words.sentence * 1.25 + 18
  const tall = (rows: number, z: number) => Math.ceil(commandTop + rows * z * 1.35 + foot + pad + margin)
  const wrapAt = (z: number) => wrapOnce(c, crewText(d.command), max, `500 ${z}px ${mono}`, held ? Infinity : 1)
  let size: number = words.command
  let command = wrapAt(size)
  const cap = Math.round(w * HELD_CAP.aspect)
  while (held && size > HELD_CAP.commandMin && tall(command.length, size) > cap) { size -= 2; command = wrapAt(size) }
  const rowPx = size * 1.35
  const h = held ? Math.min(cap, tall(command.length, size)) : tall(command.length, size)
  // Past the cap the command is a window on the paper, which a drag on its words scrolls a row at a time.
  const shown = Math.max(1, Math.floor((h - commandTop - foot - pad - margin) / rowPx))
  const scroll = held && command.length > shown ? { box: { x: margin + pad, y: commandTop, w: max, h: shown * rowPx }, maxRow: command.length - shown, rowPx } : null
  // No line on an ask's paper opens the Bot's page (T11): the page is reached from a long answer's last sheet and the palette.
  return frame(h, (c2, paper, row) => {
    let y = textTop
    c2.textBaseline = 'top'
    c2.textAlign = 'left'
    // The top line, who asks and what, stays in view whatever the window shows.
    c2.fillStyle = css(CLIPBOARD.ink)
    c2.font = `700 ${words.sentence}px ${family}`
    for (const line of lines) { c2.fillText(line, paper.x + pad, y, max); y += words.sentence * 1.25 }
    c2.font = `500 ${size}px ${mono}`
    c2.fillStyle = held ? css(CLIPBOARD.ink) : css(CLIPBOARD.faint)
    const first = scroll ? Math.max(0, Math.min(scroll.maxRow, Math.round(row))) : 0
    y = commandTop
    for (const line of command.slice(first, scroll ? first + shown : command.length)) { c2.fillText(line, paper.x + pad, y, max); y += rowPx }
    if (scroll) {
      drawMore(c2, scroll.box, first, scroll.maxRow, family)
      // A drag on the words scrolls them, so the way to throw the board is one line under its window: by the edge.
      c2.font = `600 24px ${family}`
      c2.fillStyle = css(CLIPBOARD.faint)
      c2.textAlign = 'left'
      c2.textBaseline = 'top'
      c2.fillText(i18nT('xr.board_throw_edge'), scroll.box.x, scroll.box.y + scroll.box.h + 36, scroll.box.w)
    }
  }, { scroll, note: held ? noteStrip(h, margin, pad, max, 0) : null })
}

/** The done sheet's lines, in the picture's px: its head at the Bot and in his hand, and a linked change's line. */
const DONE_PX = { headAtBot: 52, madeAtBot: 50, head: 34 } as const

/** The ink of a change's or an issue's line, by its state: open green, merged violet, closed red, else faint. */
const madeInk = (state: string): string =>
  css(state === 'open' ? CLIPBOARD.open : state === 'merged' ? CLIPBOARD.merged : state === 'closed' ? CLIPBOARD.closed : CLIPBOARD.faint)

/** A failed sheet's red edge (P3 slice 2): a band round the paper, inside its edge, so it reads at any distance. */
function drawFailedEdge(c: CanvasRenderingContext2D, paper: Px) {
  const band = Math.max(10, Math.round(paper.w * 0.018))
  c.save()
  c.strokeStyle = css(CLIPBOARD.failed)
  c.lineWidth = band
  c.strokeRect(paper.x + band / 2, paper.y + band / 2, paper.w - band, paper.h - band)
  c.restore()
}

/** The one line of a words board at a desk far off, in the picture's px: as large as fits, never smaller than `least`. */
export const DESK_LINE = { size: 112, least: 72 } as const

/**
 * A words board at a desk far off (the lead's 12:42): one large line on the paper and nothing else. A done sheet's
 * line ("done · 12:34") is in the done colour, the strip's green (`--ok`), and a failed one's in red; both are drawn
 * whole. A words board's is its first line, cut with an ellipsis when it is longer than the paper at the least size.
 * Its Bot's name comes first (D42, `deskWords`): on one line when that keeps the words as large as two lines would,
 * else the name on a line of its own over the rest, so the longer line does not shrink the words.
 */
function layDesk(desk: { line: string; name?: string; done: boolean; failed?: boolean }, c: CanvasRenderingContext2D, at: { pad: number; margin: number; max: number; family: string; paperTop: number }, frame: Frame): Laid {
  const { pad, margin, max, family, paperTop } = at
  // A done or failed line ("stopped · 3:45 PM") says the time, so it shrinks until it is whole, past the least size
  // that a words board's line keeps before it is cut.
  const least = desk.done || desk.failed ? 1 : DESK_LINE.least
  /** The largest size at which every one of `texts` fits the paper's width, no smaller than `least`. */
  const fit = (texts: readonly string[]): number => {
    let size: number = DESK_LINE.size
    c.font = `800 ${size}px ${family}`
    const wide = Math.max(...texts.map(t => c.measureText(t).width))
    if (wide > max) size = Math.max(least, Math.floor((size * max) / wide))
    c.font = `800 ${size}px ${family}`
    // Text does not narrow quite in step with its size: the size that the ratio gives can still be a pixel too wide.
    while (size > least && texts.some(t => c.measureText(t).width > max)) {
      size -= 1
      c.font = `800 ${size}px ${family}`
    }
    return size
  }
  const one = crewText(desk.name ? deskWords(desk.name, desk.line) : desk.line)
  const two = desk.name ? [crewText(desk.name), crewText(desk.line)] : null
  const sizeOne = fit([one])
  const sizeTwo = two ? fit(two) : 0
  const split = !!two && sizeTwo > sizeOne
  const size = split ? sizeTwo : sizeOne
  c.font = `800 ${size}px ${family}`
  const cut = (text: string) => {
    if (c.measureText(text).width <= max) return text
    const chars = [...text]
    while (chars.length > 1 && c.measureText(`${chars.join('').trimEnd()}\u2026`).width > max) chars.pop()
    return chars.join('').trimEnd().concat('\u2026')
  }
  const lines = (split && two ? two : [one]).map(cut)
  // Clear of the clip, which stands off the paper and, seen from below a desk far off, lies a little lower on it.
  const top = paperTop + 72
  const ink = desk.failed ? css(CLIPBOARD.failed) : desk.done ? readPalette(document.body).ok : css(CLIPBOARD.ink)
  const laid = frame(Math.ceil(top + size * 1.2 * lines.length + pad + margin), (c2, paper) => {
    c2.textBaseline = 'top'
    c2.textAlign = 'left'
    c2.fillStyle = ink
    c2.font = `800 ${size}px ${family}`
    lines.forEach((l, i) => c2.fillText(l, paper.x + pad, top + i * size * 1.2, max))
    if (desk.failed) drawFailedEdge(c2, paper)
  })
  // What is drawn, as one line of words: the two lines read as the one line they make.
  const drawn = split && desk.name && lines[1] === crewText(desk.line) && lines[0] === crewText(desk.name) ? one : lines.join(' ')
  return { ...laid, letter: size / PX.width, drawnLine: drawn }
}

/**
 * The done sheet at its Bot (P3, `d-1-at-bot`): the head ("Juno: done · 10:42") small over the answer's first
 * sentence as a board at a Bot shows it, and under it a line for each change it links in that one's state's colour.
 */
function layDoneAtBot(done: DoneFoot, lines: string[], at: { textTop: number; pad: number; margin: number; max: number; family: string }, frame: Frame): Laid {
  const { textTop, pad, margin, max, family } = at
  const sentence = WORDS.bot.sentence * 1.25
  const madeTop = textTop + DONE_PX.headAtBot * 1.3 + 8 + lines.length * sentence + 14
  const h = Math.ceil(madeTop + done.made.length * DONE_PX.madeAtBot * 1.35 + pad + margin)
  return frame(h, (c2, paper) => {
    c2.textBaseline = 'top'
    c2.textAlign = 'left'
    c2.fillStyle = css(CLIPBOARD.faint)
    c2.font = `600 ${DONE_PX.headAtBot}px ${family}`
    c2.fillText(done.head, paper.x + pad, textTop, max)
    let y = textTop + DONE_PX.headAtBot * 1.3 + 8
    c2.fillStyle = css(CLIPBOARD.ink)
    c2.font = `700 ${WORDS.bot.sentence}px ${family}`
    for (const line of lines) { c2.fillText(line, paper.x + pad, y, max); y += sentence }
    y = madeTop
    c2.font = `600 ${DONE_PX.madeAtBot}px ${family}`
    for (const m of done.made) { c2.fillStyle = madeInk(m.state); c2.fillText(m.text, paper.x + pad, y, max); y += DONE_PX.madeAtBot * 1.35 }
    if (done.failed) drawFailedEdge(c2, paper)
  })
}

/**
 * A hold's sheet, in the picture's px: its words at a sheet's body size and line (`SHEET.body`), the line that names
 * the Bot and the hint at the size of a sheet's foot line, the rows an empty sheet has, and the most rows it shows.
 */
export const SPEAK_SHEET = { small: 30, rows: 4, most: 12 } as const

/**
 * The blank sheet a Bot holds up while he holds a pinch on it (P2): a small line that names the Bot at its top, and his
 * words in the pen's hand at a sheet's body size, wrapping down the page as he speaks; the sheet grows with them,
 * never with smaller words. Until his first word a faint line at its foot says to speak, and to let go to send.
 */
function laySpeak(name: string, note: NoteDraw, listening: boolean, at: { textTop: number; pad: number; margin: number; max: number; family: string }, frame: Frame): Laid {
  const { textTop, pad, margin, max, family } = at
  const words = noteWords(note)
  const font = penFont(SHEET.body)
  const row = SHEET.body * SHEET.bodyLine
  const all = words ? wrapLines(s => measureIn(font, SHEET.body)(s) * PEN.tight, words, max - 24).lines : []
  // shortcut: past `most` rows the sheet shows the newest ones; a longer message still goes whole. Upgrade to a
  // second sheet when a spoken message that long is common.
  const lines = all.slice(-SPEAK_SHEET.most)
  const top = textTop + SPEAK_SHEET.small * 1.6
  const h = Math.ceil(top + Math.max(SPEAK_SHEET.rows, lines.length) * row + SPEAK_SHEET.small * 2 + pad + margin)
  // No recognizer: the keyboard under the sheet is the way to write, and the faint line says so.
  const hint = i18nT(listening ? 'xr.speak_hint' : 'xr.speak_hint_keys', { name })
  return frame(h, (c2, paper) => {
    c2.textBaseline = 'top'
    c2.textAlign = 'left'
    c2.fillStyle = css(CLIPBOARD.faint)
    c2.font = `600 ${SPEAK_SHEET.small}px ${family}`
    c2.fillText(i18nT('xr.speak_to', { name }), paper.x + pad, textTop, max)
    if (!lines.length) {
      c2.font = `500 ${SPEAK_SHEET.small}px ${family}`
      c2.fillText(hint, paper.x + pad, h - margin - pad - SPEAK_SHEET.small, max)
      return
    }
    c2.font = font
    c2.fillStyle = css(NOTE.ink)
    let end = 0
    lines.forEach((line, i) => { end = penLine(c2, line, paper.x + pad, top + i * row, max - 24) })
    if (note.writing) c2.fillRect(Math.min(paper.x + pad + max - 16, paper.x + pad + end + 8), top + (lines.length - 1) * row + 4, 3, SHEET.body)
  })
}

/** The strip at the paper's foot where his note is written, over `under` px kept free at the very foot. */
const noteStrip = (h: number, margin: number, pad: number, max: number, under: number, corner = 0): Px =>
  ({ x: margin + pad - 12, y: h - margin - pad - under - NOTE.h, w: max + 24 - corner, h: NOTE.h })

/** A question sheet in his hand, in the picture's px: its words, its boxes and the box's tick square. */
export const QSHEET = { head: 50, headLine: 1.25, headLines: 3, box: 84, gap: 14, tick: 52, label: 40, foot: 70 } as const

/**
 * A sheet of a Bot's question in his hand: its header small, the question large, then each choice a box drawn on the
 * paper, with a tick square and its words; a print on a box is his choice (`question.ts`). A question with no choices
 * says that his note answers it. The foot holds his note, which sheet of how many, and the turned corner.
 */
function layQuestion(q: NonNullable<Draw['question']>, name: string, c: CanvasRenderingContext2D, at: { textTop: number; pad: number; margin: number; max: number; family: string }, frame: Frame): Laid {
  const { textTop, pad, margin, max, family } = at
  const sheet = q.at
  // Its small heading names its Bot, as an approval's sentence does ("Atlas: …"): "Juno: Rollout", or the name alone
  // on a sheet with no header of its own. One line, cut at the paper's width: a long name takes none of the
  // question's room.
  const own = sheet.question.header && sheet.from === 0 ? sheet.question.header : ''
  const header = name ? wrapOnce(c, crewText(own ? `${name}: ${own}` : name), max, `600 30px ${family}`, 1)[0] ?? '' : own
  const words = wrapOnce(c, crewText(sheet.question.question), max, `700 ${QSHEET.head}px ${family}`, QSHEET.headLines)
  const top = textTop + (header ? 40 : 0)
  const boxesTop = top + QSHEET.headLines * QSHEET.head * QSHEET.headLine + 24
  const boxes: Px[] = sheet.options.map((_, i) => ({ x: margin + pad - 8, y: boxesTop + i * (QSHEET.box + QSHEET.gap), w: max + 16, h: QSHEET.box }))
  const room = Math.max(1, sheet.options.length, 2)
  const h = Math.ceil(boxesTop + room * (QSHEET.box + QSHEET.gap) + QSHEET.foot + NOTE.h + pad + margin)
  const right = PX.width - margin
  const bottom = h - margin
  const corner = q.turns ? { x: right - SHEET.corner * 1.3, y: bottom - SHEET.corner * 1.3, w: SHEET.corner * 1.3, h: SHEET.corner * 1.3 } : null
  return frame(h, (c2, paper) => {
    c2.textBaseline = 'top'
    c2.textAlign = 'left'
    if (header) {
      c2.font = `600 30px ${family}`
      c2.fillStyle = css(CLIPBOARD.faint)
      c2.fillText(name ? header : crewText(header), paper.x + pad, textTop, max)
    }
    c2.fillStyle = css(CLIPBOARD.ink)
    c2.font = `700 ${QSHEET.head}px ${family}`
    words.forEach((line, i) => c2.fillText(line, paper.x + pad, top + i * QSHEET.head * QSHEET.headLine, max))
    if (!sheet.options.length) {
      c2.font = `500 36px ${family}`
      c2.fillStyle = css(CLIPBOARD.faint)
      c2.fillText(i18nT('xr.question_free'), paper.x + pad, boxesTop + 10, max)
    }
    drawBoxes(c2, boxes, sheet.options, q.picked, q.prints, h, family, printWord('box'))
    if (sheet.of > 1) {
      c2.font = `500 30px ${family}`
      c2.fillStyle = css(CLIPBOARD.faint)
      c2.fillText(i18nT('xr.says_sheet', { n: sheet.n, of: sheet.of }), paper.x + pad, bottom - pad - 34, max)
    }
    if (corner) drawCorner(c2, right, bottom)
  }, { boxes, pageLine: corner, note: noteStrip(h, margin, pad, max, 50, corner ? 140 : 0) })
}

/**
 * Choices as boxes drawn on the paper (a question's, or the ones a Bot's words offer him): a ruled frame, a tick square
 * at its left and the choice's words; a box he chose has a green frame, and his prints stay where he pressed (D17).
 */
function drawBoxes(c2: CanvasRenderingContext2D, boxes: readonly Px[], labels: readonly string[], picked: readonly string[], prints: readonly { print: Print }[], h: number, family: string, word: string) {
  labels.forEach((label, i) => {
    const b = boxes[i]
    const on = picked.includes(label)
    c2.strokeStyle = css(on ? CLIPBOARD.approved : CLIPBOARD.faint)
    c2.lineWidth = on ? 4 : 2
    c2.beginPath(); c2.roundRect(b.x, b.y, b.w, b.h, 12); c2.stroke()
    const t = { x: b.x + 18, y: b.y + (b.h - QSHEET.tick) / 2 }
    c2.strokeStyle = css(CLIPBOARD.ink)
    c2.lineWidth = 3
    c2.strokeRect(t.x, t.y, QSHEET.tick, QSHEET.tick)
    if (on) {
      // The chosen box's square carries a green tick, so the choice reads at a glance wherever the print lies.
      const k = QSHEET.tick
      c2.strokeStyle = css(CLIPBOARD.approved)
      c2.lineWidth = 8
      c2.lineCap = 'round'
      c2.lineJoin = 'round'
      c2.beginPath(); c2.moveTo(t.x + k * 0.18, t.y + k * 0.52); c2.lineTo(t.x + k * 0.42, t.y + k * 0.78); c2.lineTo(t.x + k * 0.86, t.y + k * 0.2); c2.stroke()
      c2.lineCap = 'butt'
      c2.lineJoin = 'miter'
    }
    c2.fillStyle = css(CLIPBOARD.ink)
    c2.font = `600 ${QSHEET.label}px ${family}`
    c2.textBaseline = 'middle'
    c2.fillText(crewText(label), t.x + QSHEET.tick + 22, b.y + b.h / 2 + 2, b.w - QSHEET.tick - 60)
    c2.textBaseline = 'top'
  })
  for (const p of prints) drawPrint(c2, { x: p.print.u * PX.width, y: p.print.v * h }, p.print.tilt, p.print.seed, 1, word)
}

/** The turned corner of a sheet: the paper folded up at its lower right, the board's blue under it, a crease along it. */
function drawCorner(c2: CanvasRenderingContext2D, right: number, bottom: number) {
  const k = SHEET.corner
  c2.fillStyle = css(CLIPBOARD.board)
  c2.beginPath(); c2.moveTo(right, bottom - k); c2.lineTo(right, bottom); c2.lineTo(right - k, bottom); c2.closePath(); c2.fill()
  c2.fillStyle = css(CLIPBOARD.fold)
  c2.beginPath(); c2.moveTo(right, bottom - k); c2.lineTo(right - k, bottom - k); c2.lineTo(right - k, bottom); c2.closePath(); c2.fill()
  c2.strokeStyle = 'rgba(0, 0, 0, 0.18)'
  c2.lineWidth = 2
  c2.beginPath(); c2.moveTo(right, bottom - k); c2.lineTo(right - k, bottom); c2.stroke()
}

/** A sheet of words in his hand, in the picture's px: the first sentence's and the body's sizes and line heights. */
export const SHEET = { head: 50, headLine: 1.25, body: 44, bodyLine: 1.3, foot: 110 + NOTE.h, corner: 96 } as const

/** A canvas only to measure text by, made once. */
let ruler: CanvasRenderingContext2D | null | undefined
const measureIn = (font: string, size: number) => {
  if (ruler === undefined) ruler = typeof document === 'undefined' ? null : document.createElement('canvas').getContext('2d')
  const c = ruler
  // shortcut: with no canvas (a test's DOM) a character is taken as about half the size wide.
  return c ? (s: string) => { c.font = font; return c.measureText(s).width } : (s: string) => [...s].length * size * 0.52
}

/** How a sheet's text breaks into the lines its picture draws (`says.sheetsOf` pages by these). */
export function sheetWrap(): { head: (s: string) => string[]; body: (s: string) => string[] } {
  const max = PX.width - 2 * PX.margin - 2 * PX.pad
  const family = FAMILY()
  const head = measureIn(`700 ${SHEET.head}px ${family}`, SHEET.head)
  const body = measureIn(`500 ${SHEET.body}px ${family}`, SHEET.body)
  return { head: s => wrapLines(head, crewText(s), max).lines, body: s => wrapLines(body, crewText(s), max).lines }
}

/** What a sheet that counts what is left out says, a line for each: "More words: 1700", "Tables: 1", "Code blocks: 2". */
export function leftWords(left: NonNullable<Sheet['left']>): string[] {
  const parts: string[] = []
  if (left.words > 0) parts.push(i18nT('xr.says_left_words', { count: left.words }))
  if (left.tables > 0) parts.push(i18nT('xr.says_left_tables', { count: left.tables }))
  if (left.code > 0) parts.push(i18nT('xr.says_left_code', { count: left.code }))
  return parts
}

/** Where a sheet's words, counts, choices and page mark end, from the paper's top, in px. */
function sheetEnds(s: NonNullable<Draw['sheet']>, name: string, at: { textTop: number; pad: number; margin: number; max: number; family: string }) {
  const { textTop, pad, margin, max, family } = at
  const sheet = s.at
  const row = SHEET.body * SHEET.bodyLine
  const counts = sheet.left ? leftWords(sheet.left) : []
  const tail = sheet.tail ?? []
  const done = sheet.done
  const wordsTop = textTop + (done ? row : 0)
  const wordsEnd = wordsTop + sheet.head.length * SHEET.head * SHEET.headLine + (sheet.head.length ? 10 : 0) + (sheet.body.length + tail.length) * row
  const doneRows = done ? done.made.length + done.left.length : 0
  const choicesTop = wordsEnd + (tail.length ? 12 : 0) + (doneRows ? doneRows * row + 12 : 0) + counts.length * row + 12
  const choices = sheet.choices ?? []
  const textEnd = choices.length ? choicesTop + choices.length * (QSHEET.box + QSHEET.gap) : choicesTop - 12
  const markText = i18nT('xr.board_page', { name })
  const mark = sheet.left || done?.working || done?.pageFoot ? { x: margin + pad - 12, y: textEnd + 8, w: Math.min(max, measureIn(`600 36px ${family}`, 36)(markText)) + 24, h: 76 } : null
  return { row, counts, tail, done, wordsTop, doneRows, choicesTop, choices, textEnd, markText, mark }
}

/** How tall a board of one sheet is: its words, a row of air, the note line and what stands under it (`noteStrip`). */
const SHORT_SHEET = { air: 14, under: 50, underShort: 8 } as const
const shortSheetH = (s: NonNullable<Draw['sheet']>, name: string, at: { textTop: number; pad: number; margin: number; max: number; family: string }): number => {
  const e = sheetEnds(s, name, at)
  return Math.ceil((e.mark ? e.mark.y + e.mark.h : e.textEnd) + (e.done?.working ? 0 : SHORT_SHEET.air + NOTE.h + SHORT_SHEET.underShort) + at.pad + at.margin)
}

/**
 * A sheet of a Bot's words in his hand: the first sentence large on its first sheet, the words under it, at the foot
 * which sheet of how many, and the paper's turned corner at the lower right (the press that shows the next sheet).
 * Every sheet of the board is as tall, so the board does not change size as he turns. The sheet that counts what is
 * left out has the one mark that opens the Bot's page.
 */
function laySheet(s: NonNullable<Draw['sheet']>, name: string, at: { textTop: number; pad: number; margin: number; max: number; family: string }, frame: Frame): Laid {
  const { textTop, pad, margin, max, family } = at
  const sheet = s.at
  const textH = SAYS.headLines * SHEET.head * SHEET.headLine + (SAYS.lines - SAYS.headLines) * SHEET.body * SHEET.bodyLine
  const full = Math.ceil(textTop + textH + SHEET.foot + pad + margin)
  // A board of one sheet is as tall as its words and its note line (D82 1): a sheet of three lines is a short sheet.
  const h = sheet.of > 1 || sheet.before || s.turns ? full : Math.min(full, shortSheetH(s, name, at))
  const right = PX.width - margin
  const bottom = h - margin
  const corner = s.turns ? { x: right - SHEET.corner * 1.3, y: bottom - SHEET.corner * 1.3, w: SHEET.corner * 1.3, h: SHEET.corner * 1.3 } : null
  const { row, counts, tail, done, wordsTop, doneRows, choicesTop, choices, markText, mark } = sheetEnds(s, name, at)
  const boxes: Px[] = choices.map((_, i) => ({ x: margin + pad - 8, y: choicesTop + i * (QSHEET.box + QSHEET.gap), w: max + 16, h: QSHEET.box }))
  return frame(h, (c2, paper) => {
    let y = textTop
    c2.textBaseline = 'top'
    c2.textAlign = 'left'
    if (done?.failed) drawFailedEdge(c2, paper)
    if (done) {
      c2.fillStyle = css(done.failed ? CLIPBOARD.failed : CLIPBOARD.faint)
      c2.font = `600 ${DONE_PX.head}px ${family}`
      c2.fillText(done.head, paper.x + pad, y + (row - DONE_PX.head) / 2, max)
      y = wordsTop
    }
    c2.fillStyle = css(CLIPBOARD.ink)
    c2.font = `700 ${SHEET.head}px ${family}`
    for (const line of sheet.head) { c2.fillText(line, paper.x + pad, y, max); y += SHEET.head * SHEET.headLine }
    if (sheet.head.length) y += 10
    c2.font = `500 ${SHEET.body}px ${family}`
    for (const line of [...sheet.body, ...tail]) { c2.fillText(line, paper.x + pad, y, max); y += row }
    if (tail.length) y += 12
    if (done && doneRows) {
      // What it links, each in its state's colour, and opens nothing (D35); then what it left open.
      y += 12
      c2.font = `600 ${SHEET.body}px ${family}`
      for (const m of done.made) { c2.fillStyle = madeInk(m.state); c2.fillText(m.text, paper.x + pad, y, max); y += row }
      c2.fillStyle = css(CLIPBOARD.faint)
      c2.font = `500 ${SHEET.body}px ${family}`
      for (const line of done.left) { c2.fillText(line, paper.x + pad, y, max); y += row }
    }
    c2.font = `600 ${SHEET.body}px ${family}`
    c2.fillStyle = css(CLIPBOARD.faint)
    for (const line of counts) { c2.fillText(line, paper.x + pad, y, max); y += row }
    // The choices the Bot offers him, never cut away: boxes that the stamp marks, as a question's.
    if (choices.length) drawBoxes(c2, boxes, choices, s.picked ?? [], s.prints ?? [], h, family, printWord(done?.failed ? 'retry' : 'box'))
    if (mark) {
      // The way to the Bot's real page: a plain line of print, underlined, on the sheet that counts what is left out.
      c2.font = `600 36px ${family}`
      c2.fillStyle = css(CLIPBOARD.ink)
      const tw = Math.min(max, c2.measureText(markText).width)
      c2.fillText(markText, mark.x + 12, mark.y + 16, max)
      c2.fillRect(mark.x + 12, mark.y + 60, tw, 3)
    }
    // Which sheet of how many, at the foot.
    if (sheet.of > 1 || sheet.before) {
      c2.font = `500 30px ${family}`
      c2.fillStyle = css(CLIPBOARD.faint)
      c2.fillText(i18nT(sheet.before ? 'xr.says_sheet_before' : 'xr.says_sheet', { n: sheet.n, of: sheet.of }), paper.x + pad, bottom - pad - 34, max)
    }
    if (corner) drawCorner(c2, right, bottom)
  }, { pageLine: corner, mark, boxes, note: done?.working ? null : noteStrip(h, at.margin, pad, max, h < full ? SHORT_SHEET.underShort : SHORT_SHEET.under, corner ? 140 : 0) })
}

/**
 * The window's signs: a thin bar at its right that shows where it stands in the whole, and, while there is more
 * below, a fade over its last row with a small line that says so.
 */
function drawMore(c: CanvasRenderingContext2D, box: Px, first: number, maxRow: number, family: string) {
  const rows = maxRow + box.h
  const barX = box.x + box.w + 14
  c.fillStyle = 'rgba(29, 35, 48, 0.12)'
  c.fillRect(barX, box.y, 6, box.h)
  const barH = Math.max(24, (box.h * box.h) / rows)
  c.fillStyle = css(CLIPBOARD.faint)
  c.fillRect(barX, box.y + ((box.h - barH) * first) / Math.max(1, maxRow), 6, barH)
  if (first >= maxRow) return
  const fade = c.createLinearGradient(0, box.y + box.h - 56, 0, box.y + box.h)
  fade.addColorStop(0, 'rgba(247, 245, 239, 0)')
  fade.addColorStop(1, 'rgba(247, 245, 239, 0.96)')
  c.fillStyle = fade
  c.fillRect(box.x - 4, box.y + box.h - 56, box.w + 8, 56)
  c.font = `600 24px ${family}`
  c.fillStyle = css(CLIPBOARD.faint)
  c.textAlign = 'right'
  c.textBaseline = 'top'
  c.fillText(i18nT('xr.board_more_below'), box.x + box.w, box.y + box.h + 4)
  c.textAlign = 'left'
}

/**
 * A board's picture as a texture. It is laid out again only when what it shows changes; a scroll of the command's
 * window (`row`) paints the same canvas again, so the canvas keeps one size however long the command is. A scroll is
 * painted at most once a frame, with the row it has at that frame: a hand's moves can come several to a frame, and
 * each paint uploads the whole picture again.
 */
/** How many times a board's picture was painted: a row reads it to see that a scroll paints at most once a frame. */
export const boardPaints = { count: 0 }

export function useBoardPicture(d: Draw, row = 0): BoardPic & { canvas: HTMLCanvasElement } {
  const key = JSON.stringify(d)
  const pic = useMemo(() => {
    const canvas = document.createElement('canvas')
    // Kept in memory, not on the GPU: a leaving board reads its colours back (`BoardDust`), and a read of a canvas the
    // GPU holds took about a second on the main thread.
    const c = canvas.getContext('2d', { willReadFrequently: true })
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    if (!c) { canvas.width = 1; canvas.height = 1; return { texture, canvas, c, laid: null, aspect: 1, paper: { u0: 0, v0: 0, u1: 1, v1: 1 }, pageLine: null, pageMark: null, scroll: null, boxes: [], noteLine: null, deskLetter: 0, deskDrawn: '' } }
    const laid = layBoard(d, c)
    canvas.width = laid.w
    canvas.height = Math.max(1, laid.h)
    const box = (b: Px): UvBox => ({ u0: b.x / laid.w, v0: b.y / canvas.height, u1: (b.x + b.w) / laid.w, v1: (b.y + b.h) / canvas.height })
    const scroll = laid.scroll ? { box: box(laid.scroll.box), maxRow: laid.scroll.maxRow, rowV: laid.scroll.rowPx / canvas.height } : null
    return { texture, canvas, c, laid, aspect: canvas.height / canvas.width, paper: box(laid.paper), pageLine: laid.pageLine ? box(laid.pageLine) : null, pageMark: laid.mark ? box(laid.mark) : null, scroll, boxes: (laid.boxes ?? []).map(box), noteLine: laid.note ? box(laid.note) : null, deskLetter: laid.letter ?? 0, deskDrawn: laid.drawnLine ?? '' }
    // `key` stands for what the picture is drawn from.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  // A new picture is painted before the next frame is drawn, with the window at its row; a scroll, at the next frame.
  const painted = useRef<{ pic: BoardPic | null; row: number }>({ pic: null, row: 0 })
  const paint = (r: number) => {
    if (!pic.c || !pic.laid) return
    pic.laid.paint(pic.c, r)
    pic.texture.needsUpdate = true
    painted.current = { pic, row: r }
    boardPaints.count += 1
  }
  const want = useRef(row)
  want.current = row
  useLayoutEffect(() => { if (painted.current.pic !== pic) paint(want.current) })
  useFrame(() => { if (painted.current.pic === pic && painted.current.row !== want.current) paint(want.current) })
  useEffect(() => () => pic.texture.dispose(), [pic])
  return pic
}

/**
 * Where a stamp let go at `at` prints, on the paper of a picture `aspect` tall to its width. A stamp whose face touches
 * the paper prints, moved in just enough that the whole print lies on the paper at its most turn; one whose face misses
 * the paper prints nothing (`null`), and the stamp goes back.
 */
export function printPlace(at: Uv, paper: UvBox, aspect: number): Uv | null {
  const hPx = PX.width * aspect
  const x = at.u * PX.width
  const y = at.v * hPx
  const p = { x0: paper.u0 * PX.width, x1: paper.u1 * PX.width, y0: paper.v0 * hPx, y1: paper.v1 * hPx }
  if (x + PRINT.w / 2 < p.x0 || x - PRINT.w / 2 > p.x1 || y + PRINT.h / 2 < p.y0 || y - PRINT.h / 2 > p.y1) return null
  const sin = Math.sin(PRINT.tiltMax)
  const cos = Math.cos(PRINT.tiltMax)
  const mu = (PRINT.w / 2) * cos + (PRINT.h / 2) * sin
  const mv = (PRINT.h / 2) * cos + (PRINT.w / 2) * sin
  const into = (v: number, lo: number, hi: number) => (lo > hi ? (lo + hi) / 2 : Math.min(hi, Math.max(lo, v)))
  return { u: into(x, p.x0 + mu, p.x1 - mu) / PX.width, v: into(y, p.y0 + mv, p.y1 - mv) / hPx }
}
