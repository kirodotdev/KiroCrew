import { describe, expect, it } from 'vitest'
import type { XrNeed } from '../model'
import { boardsNow, speakBoard } from './board'
import { HOLD, speakEnd } from './speak'

const approval = (id: string): XrNeed => ({
  id, kind: 'approval', card: true, bot: 'juno', who: 'Juno', topic: '', slot: 's1', action: 'Run it?', tool: 'bash', command: 'ls', extra: [],
  version: `${id}|v1`, purpose: '', origin: 'native', requestId: `r-${id}`, requestMid: '', askId: '', ts: 1, questions: [],
})

describe('speak to a Bot (P2)', () => {
  it('holds for the click\'s own time, with the drag\'s own slop', () => {
    // The recognizer starts 300 ms after the sheet, so a drag never opens the microphone (review, 09:10).
    expect(HOLD).toEqual({ ms: 500, slopPx: 8, slopM: 0.015, listenAfterMs: 300 })
  })

  it('sends one message only at his release with words, and the words are what the sheet showed', () => {
    expect(speakEnd('let', 'Draft the notes', 'for the beta')).toEqual({ end: 'sent', words: 'Draft the notes for the beta' })
    expect(speakEnd('let', '', 'still heard')).toEqual({ end: 'sent', words: 'still heard' })
    expect(speakEnd('let', '  ', '')).toEqual({ end: 'silent', words: '' })
    // A drag and a hold that was cut off send nothing; their words are kept for the next hold.
    expect(speakEnd('drag', 'Draft', '')).toEqual({ end: 'moved', words: 'Draft' })
    expect(speakEnd('cancel', 'Draft', '')).toEqual({ end: 'cut', words: 'Draft' })
  })

  it('stands at its Bot while he holds it, and no ask of that Bot comes past it (D24)', () => {
    const sheet = speakBoard('juno', 0)
    expect(sheet).toMatchObject({ kind: 'speak', where: 'bot', writing: false, comment: '' })
    expect(speakBoard('juno', 1, 'kept words').comment).toBe('kept words')
    const crew = [{ slug: 'juno', needs: [approval('a')] }]
    const { boards, gone } = boardsNow(new Map([[sheet.needId, sheet]]), crew)
    expect([...boards.values()].map(b => b.kind)).toEqual(['speak'])
    expect(gone).toEqual([])
  })

  it('drops a hold\'s sheet that no hold holds, so its Bot\'s next ask raises its board (review M1, D24)', () => {
    const sheet = speakBoard('juno', 0)
    const crew = [{ slug: 'juno', needs: [approval('a')] }]
    const was = new Map([[sheet.needId, sheet]])
    expect([...boardsNow(was, crew, new Set(), undefined, new Map(), new Set([sheet.needId])).boards.keys()]).toEqual([sheet.needId])
    expect([...boardsNow(was, crew, new Set(), undefined, new Map(), new Set()).boards.keys()]).toEqual(['a'])
  })
})
