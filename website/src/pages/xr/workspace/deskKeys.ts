/**
 * The keyboard of the room, as plain numbers: which keys it has and where each lies on its board. `DeskKeys.tsx`
 * draws it. With a desk (`desk=1`) it lies on the desk's near band, where the hands rest, and a finger's touch
 * types (`deskTouch.ts`); with none it stands in front of the person and the dot with a pinch types.
 *
 * A board is ten keys wide and four rows deep. Its own frame: `x` to the right of its middle, `y` away from the
 * person (up, on a board that stands), in metres.
 */
import { CARD } from './chatCard'
import { DESK } from './desk'
import { signs } from './homeNames'

export const KEYS = {
  columns: 10,
  rows: 4,
  /**
   * From one key to the next. On the desk a finger's tip is known to about a centimetre, so a key is twice a real
   * one; a board that stands is aimed at with the head, so it stands farther; its keys are as large at the eye as a line of a page.
   */
  deskStepM: 0.036,
  standStepM: 0.034,
  /** The air between two keys, as a share of the step. */
  gapShare: 0.1,
  /** The board reaches this far past its outer keys: a press there, as one between two keys, is the board's and does nothing. */
  marginM: 0.015,
  /** On the desk: from the near edge to the board's near edge. */
  deskNearM: 0.02,
  /** Standing: how far it is, and how far under the eyes' level; it stands under the card. */
  standM: 0.8,
  standYawDeg: CARD.outYawDeg,
  /**
   * -12.5, not -11: at -11 the line over the keys reached -3.4 degrees and the card's lower edge stands at -3.5, so
   * the keys' line, drawn in its own band over the room's, lay over the card. Now a degree and more under it (M87).
   */
  standPitchDeg: -12.5,
  /** The box over a key that takes a pointer, and the one that a finger's touch counts as near, are this tall. */
  aimM: 0.012,
  touchM: 0.03,
} as const

/**
 * The line over the keys' far row (D97 1): how many of his last signs it shows, its air to that row as a share of the
 * step, and the most of its width that what is said beside his letters may take (M68).
 */
export const KEYS_LINE = { signs: 30, gapShare: 0.25, saidShare: 0.62 } as const

/**
 * What the line over the keys shows of `text`: all of it, or its last `KEYS_LINE.signs` whole signs with "…" in
 * front. `fits`: whether a line has room where it is drawn; while it has none, one more sign in front goes, so the
 * letters stay as large as the keys' own.
 */
export function keysLine(text: string, fits: (shown: string) => boolean = () => true): string {
  const all = signs(text)
  let last = Math.min(all.length, KEYS_LINE.signs)
  const shown = () => (last === all.length ? text : `…${all.slice(-last).join('')}`)
  while (last > 1 && !fits(shown())) last -= 1
  return shown()
}

/** A text that fits: whole, or its first whole signs with "…" at its end. Never squeezed (M68). */
export function cutToFit(text: string, fits: (shown: string) => boolean): string {
  if (fits(text)) return text
  const all = signs(text)
  let n = all.length - 1
  while (n > 1 && !fits(`${all.slice(0, n).join('').trimEnd()}…`)) n -= 1
  return `${all.slice(0, n).join('').trimEnd()}…`
}

/** Which letters show: small, capital, or numbers and signs. */
export type KeysLayer = 'abc' | 'ABC' | '123'

/** One key: what it types (`key`: one character, or a name), what it shows, and where it lies in the grid. */
export interface Key { id: string; key: string; label: string; col: number; row: number; span: number }

const LETTERS = ['qwertyuiop', 'asdfghjkl', 'zxcvbnm,.']
const SIGNS = ['1234567890', '-/:;()$&@', '\'"!+=*%_#~']

/** The keys of a layer. Row 0 is the far row. The names: `Backspace`, `Enter`, `Shift`, `Layer`. */
export function keysOf(layer: KeysLayer): Key[] {
  const rows = layer === '123' ? SIGNS : LETTERS
  const out: Key[] = []
  const add = (key: string, label: string, col: number, row: number, span = 1) => out.push({ id: `${row}:${col}`, key, label, col, row, span })
  const char = (c: string) => (layer === 'ABC' ? c.toUpperCase() : c)
  ;[...rows[0]].forEach((c, i) => add(char(c), char(c), i, 0))
  ;[...rows[1]].forEach((c, i) => add(char(c), char(c), i, 1))
  add('Backspace', '\u232B', 9, 1)
  // The signs have no capitals: their third row has a sign where the letters have Shift.
  if (layer !== '123') add('Shift', '\u21E7', 0, 2)
  ;[...rows[2]].forEach((c, i) => add(char(c), char(c), layer === '123' ? i : i + 1, 2))
  add('Layer', layer === '123' ? 'abc' : '123', 0, 3, 2)
  add(' ', '', 2, 3, 5)
  add('?', '?', 7, 3)
  add('Enter', '\u23CE', 8, 3, 2)
  return out
}

/** The board's size for a step from key to key. */
export const boardSize = (stepM: number): [number, number] => [KEYS.columns * stepM, KEYS.rows * stepM]

/** A key's middle and size on its board. */
export function keyRect(key: Key, stepM: number): { x: number; y: number; w: number; h: number } {
  const gap = stepM * KEYS.gapShare
  return {
    x: (key.col + key.span / 2 - KEYS.columns / 2) * stepM,
    y: (KEYS.rows / 2 - key.row - 0.5) * stepM,
    w: key.span * stepM - gap,
    h: stepM - gap,
  }
}

/** The layer after a key: Shift goes between small and capital; a letter typed in capitals goes back to small; `Layer` goes to the signs and back. */
export function layerAfter(layer: KeysLayer, key: string): KeysLayer {
  if (key === 'Shift') return layer === 'abc' ? 'ABC' : 'abc'
  if (key === 'Layer') return layer === '123' ? 'abc' : '123'
  return layer === 'ABC' ? 'abc' : layer
}

/** Where the board's middle lies on the desk, in from the near edge: inside the band that the hands rest on. */
export const deskKeysV = (): number => KEYS.deskNearM + boardSize(KEYS.deskStepM)[1] / 2

/** Whether a board of the desk's step fits the hands' rest. */
export const fitsRest = (): boolean => KEYS.deskNearM + boardSize(KEYS.deskStepM)[1] <= DESK.restM && boardSize(KEYS.deskStepM)[0] <= DESK.widthM
