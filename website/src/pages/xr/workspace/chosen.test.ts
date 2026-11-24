import { afterEach, describe, expect, it } from 'vitest'
import { boardFor, boardsNow, takeBoard } from './board'
import { chosenOn, keepChosen } from './chosen'
import type { XrNeed } from '../model'

const print = { u: 0.4, v: 0.6, tilt: -0.1, seed: 3 }

describe('a choice printed on a Bot\'s words (review, 07:46)', () => {
  afterEach(() => window.localStorage.clear())

  it('is kept by the answer\'s own key, so the same words after a reload take no second print', () => {
    keepChosen('juno', '2026-11-21T07:00:00|0', { label: 'Ship it now', print })
    // A new page reads the same store: the print is there for that answer, and for no other.
    expect(chosenOn('juno', '2026-11-21T07:00:00|0')).toEqual({ label: 'Ship it now', print })
    expect(chosenOn('juno', '2026-11-21T07:05:00|0')).toBeNull()
    expect(chosenOn('mira', '2026-11-21T07:00:00|0')).toBeNull()
    // A send that failed takes it back.
    keepChosen('juno', '2026-11-21T07:00:00|0', null)
    expect(chosenOn('juno', '2026-11-21T07:00:00|0')).toBeNull()
  })
})

describe('a note on an ask that has left the list (review, 07:46)', () => {
  it('stays in his hand with the note; a board with none ends', () => {
    const need = { id: 'q', kind: 'question', bot: 'mira', version: 'q|v1', askId: 'a', questions: [{ header: '', question: 'A?', options: ['x'] }] } as unknown as XrNeed
    const held = takeBoard(boardFor(need), need, 0)
    const noted = { ...held, comment: 'later' }
    expect([...boardsNow(new Map([['q', noted]]), [{ slug: 'mira', needs: [] }]).boards.keys()]).toEqual(['q'])
    expect(boardsNow(new Map([['q', held]]), [{ slug: 'mira', needs: [] }]).gone.map(b => b.needId)).toEqual(['q'])
  })
})
