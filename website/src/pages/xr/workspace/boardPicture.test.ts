import { describe, expect, it } from 'vitest'
import { layBoard, PRINT_FACE, printPlace, printTilt, printWord } from './boardPicture'

/** A 2D context that measures a mono face (0.6 em a character) and keeps every line of text drawn. */
function fakeContext() {
  const drawn: string[] = []
  const size = () => Number(/(\d+)px/.exec(c.font)?.[1] ?? 16)
  const c = {
    font: '', fillStyle: '', strokeStyle: '', lineWidth: 1, textAlign: 'left', textBaseline: 'top', globalAlpha: 1,
    measureText: (s: string) => ({ width: s.length * size() * 0.6 }),
    fillText: (s: string) => { drawn.push(s) },
    clearRect: () => {}, fillRect: () => {}, beginPath: () => {}, roundRect: () => {}, fill: () => {}, stroke: () => {}, arc: () => {},
    save: () => {}, restore: () => {}, translate: () => {}, rotate: () => {},
    createLinearGradient: () => ({ addColorStop: () => {} }),
  }
  return { c: c as unknown as CanvasRenderingContext2D, drawn }
}
const longCommand = (chars: number) => {
  const steps: string[] = []
  for (let k = 0; steps.join(' && \\\n  ').length < chars; k += 1) steps.push(`npm run release -- --stage ${k + 1} --region us-west-2 --notes "step ${k + 1} of the release"`)
  return steps.join(' && \\\n  ')
}

describe('a held board with a long command', () => {
  it('stays at its capped height, and scrolls its words in a window that reaches the last line', () => {
    for (const chars of [10_000, 50_000]) {
      const { c, drawn } = fakeContext()
      const command = longCommand(chars)
      const laid = layBoard({ kind: 'held', sentence: 'Mira: Run the release script', command, name: 'Mira', print: null }, c)
      // 0.9 of the 1024 px picture: 0.45 m on the 0.5 m held board, however long the command.
      expect(laid.h).toBeLessThanOrEqual(Math.round(1024 * 0.9))
      expect(laid.scroll?.maxRow).toBeGreaterThan(0)
      drawn.length = 0
      laid.paint(c, laid.scroll!.maxRow)
      // The top line stays in view, and the window at its last row shows the command's last step.
      expect(drawn).toContain('Mira: Run the release script')
      // A wrapped row breaks at a space, so the rows drawn, joined by one, hold the step's words in order.
      expect(drawn.join(' ')).toContain(`"step ${command.split('&&').length} of the release"`)
    }
  })
  it('grows with a command that fits, and has no window then', () => {
    const { c } = fakeContext()
    const laid = layBoard({ kind: 'held', sentence: 'Mira: Run the release script', command: 'npm run release', name: 'Mira', print: null }, c)
    expect(laid.scroll).toBeNull()
    expect(laid.h).toBeLessThan(Math.round(1024 * 0.9))
  })
})

// A held picture's paper: 40 px of board round it on 1024 px, under a 64 px top, on a picture 0.5 as tall as wide.
const aspect = 0.5
const paper = { u0: 40 / 1024, v0: 64 / 512, u1: 984 / 1024, v1: 472 / 512 }

describe('a print', () => {
  it('lands where the stamp was let go when it lies whole on the paper there', () => {
    expect(printPlace({ u: 0.5, v: 0.55 }, paper, aspect)).toEqual({ u: 0.5, v: 0.55 })
  })
  it('is moved in just enough to lie whole on the paper when the stamp\'s face touches the paper near its edge', () => {
    const half = PRINT_FACE.u / 2
    const near = printPlace({ u: paper.u1 - half / 2, v: paper.v1 - 0.02 }, paper, aspect)
    expect(near).not.toBeNull()
    expect(near!.u).toBeLessThan(paper.u1 - half)
    expect(near!.v).toBeLessThan(paper.v1 - 0.1)
    // Moved in again, it stays where it is.
    expect(printPlace(near!, paper, aspect)).toEqual(near)
  })
  it('prints nothing when the stamp\'s face misses the paper', () => {
    expect(printPlace({ u: paper.u1 + PRINT_FACE.u, v: 0.55 }, paper, aspect)).toBeNull()
    expect(printPlace({ u: 0.5, v: 0.02 }, paper, aspect)).toBeNull()
  })
  it('is turned a little to the left, never straight and never far', () => {
    expect(printTilt(0)).toBeLessThan(0)
    expect(printTilt(0.999)).toBeGreaterThan(-0.23)
  })
})

describe('the stamp\'s print (D17, D42, D44)', () => {
  it('an approval prints the green APPROVED', () => {
    expect(printWord('approval')).toBe('APPROVED')
  })
  it('a question\'s or a choice\'s box prints APPROVED too', () => {
    expect(printWord('box')).toBe('APPROVED')
  })
  it('only the failed sheet\'s box prints its own word, TRY AGAIN', () => {
    expect(printWord('retry')).toBe('TRY AGAIN')
  })
})

describe('a named desk line (D42)', () => {
  const desk = (name: string, line: string) => ({ kind: 'bot' as const, sentence: '', command: '', name, print: null, desk: { line, name, done: true } })
  it('puts the name on its own line when one line would shrink the words, so the words stay as large as before', () => {
    const before = fakeContext()
    const alone = layBoard({ kind: 'bot', sentence: '', command: '', name: 'Mira', print: null, desk: { line: 'done · 8:07 PM', done: true } }, before.c)
    const { c, drawn } = fakeContext()
    const named = layBoard(desk('Mira', 'done · 8:07 PM'), c)
    named.paint(c, 0)
    expect(drawn).toEqual(['Mira', 'done · 8:07 PM'])
    expect(named.letter).toBe(alone.letter)
    expect(named.drawnLine).toBe('Mira · done · 8:07 PM')
    expect(named.h).toBeGreaterThan(alone.h)
  })
  it('keeps one line when it fits whole at the largest size', () => {
    const { c, drawn } = fakeContext()
    const named = layBoard(desk('Al', 'done'), c)
    named.paint(c, 0)
    expect(drawn).toEqual(['Al · done'])
  })
})
