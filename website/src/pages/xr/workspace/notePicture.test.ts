import { describe, expect, it } from 'vitest'
import { penLine, penPieces } from './notePicture'

/** A 2D context that records what it is told to draw, each piece with the slant it was drawn under. */
const recorder = () => {
  const drawn: { text: string; slant: number }[] = []
  let slant = 0
  const stack: number[] = []
  const c = {
    measureText: (t: string) => ({ width: [...t].length * 10 }),
    save: () => { stack.push(slant) },
    restore: () => { slant = stack.pop() ?? 0 },
    translate: () => undefined,
    rotate: () => undefined,
    scale: () => undefined,
    transform: (_a: number, _b: number, k: number) => { slant += k },
    fillText: (text: string) => { drawn.push({ text, slant }) },
  }
  return { c: c as unknown as CanvasRenderingContext2D, drawn }
}

describe('the pen hand', () => {
  it('draws a word of a script whose letters join as one slanted piece; Latin and Chinese stay letter by letter (review, 07:20)', () => {
    for (const line of ['नमस्ते दुनिया', 'আমি কাল দেখব']) {
      const { c, drawn } = recorder()
      penLine(c, line, 0, 0, 1000)
      const words = drawn.filter(d => d.text.trim())
      // Every word whole, never cut into its letters or marks, and each under the pen's slant.
      expect(words.map(d => d.text), line).toEqual(line.split(' '))
      expect(words.every(d => d.slant < 0), line).toBe(true)
    }
    expect(penPieces('Ship it').map(p => p.text)).toEqual(['S', 'h', 'i', 'p', ' ', 'i', 't'])
    expect(penPieces('明天发').map(p => p.text)).toEqual(['明', '天', '发'])
    // An emoji with its skin tone and a letter with a separate accent mark stay one piece each (review, 07:46).
    expect(penPieces('ok 👍🏽 cafe\u0301').map(p => p.text)).toEqual(['o', 'k', ' ', '👍🏽', ' ', 'c', 'a', 'f', 'e\u0301'])
    // A word wobbles as one: its index is its first letter's place in the line.
    expect(penPieces('ok नमस्ते').map(p => [p.text, p.i])).toEqual([['o', 0], ['k', 1], [' ', 2], ['नमस्ते', 3]])
  })
})
