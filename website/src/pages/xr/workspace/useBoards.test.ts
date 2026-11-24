import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import type { XrBot, XrNeed } from '../model'
import { BOARD, STAY_MOVED, cancelMoved } from './board'
import { NOTE_SEND_WAIT_MS, noteSendId, useBoards } from './useBoards'
import { cutRetry } from '../writes'

vi.mock('../writes', async real => ({ ...(await real<typeof import('../writes')>()), cutRetry: vi.fn(() => Promise.resolve()) }))

const need = (id: string): XrNeed => ({
  id, kind: 'approval', card: true, bot: 'mira', who: 'Mira', topic: 'release', slot: 's1', action: 'Run the release script', tool: 'shell',
  command: 'npm run release', extra: [], version: `${id}|m1|npm run release`, purpose: '', origin: 'native', requestId: `r-${id}`, requestMid: 'm1', questions: [], ts: 1,
})
// The hook reads a Bot's slug, label and needs only.
const mira = (needs: XrNeed[]) => ({ slug: 'mira', label: 'Mira', needs }) as unknown as XrBot

afterEach(() => { vi.restoreAllMocks() })

describe('useBoards', () => {
  it('lets its book of answered asks be read, and an answered ask that left the list is gone from it', async () => {
    let clock = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    const decide = vi.fn(() => Promise.resolve('approved' as const))
    const host = { say: () => {}, openBot: () => {} }
    const { result, rerender } = renderHook(({ crew }) => useBoards(crew, host, decide), { initialProps: { crew: [mira([need('a')])] } })
    act(() => { result.current.take('a') })
    clock += BOARD.calmMs + 1
    await act(async () => { result.current.stamp('a', { u: 0.6, v: 0.6 }) })
    expect(decide).toHaveBeenCalledTimes(1)
    expect(result.current.answered().has('a')).toBe(true)
    // Thrown: the board is gone, and the ask, still listed until the list is read again, keeps its place in the book.
    act(() => { result.current.release('a', { px: 380, pxPerMs: 4, lastMs: 8, onBot: false }) })
    expect(result.current.probe().boards).toEqual([])
    expect(result.current.answered().has('a')).toBe(true)
    rerender({ crew: [mira([])] })
    expect(result.current.answered().has('a')).toBe(false)
  })

  it('sends "Try again" once, to that one cut session: a second print and a throw after it send nothing more (P3 slice 2)', async () => {
    let clock = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    vi.mocked(cutRetry).mockClear()
    const done = { head: 'Mira: stopped · 10:42', short: 'stopped · 10:42', made: [], left: [], failed: { cut: 'w1@t' } }
    const sheet = { head: ['Not signed in.'], body: [], n: 1, of: 1, before: false, left: null, choices: ['Try again'], done }
    const words = new Map([['mira', { id: 'w:w1', version: 'fail|w1@t', slot: 'w1', raise: true, state: 'ok' as const, sentence: 'Not signed in.', sheets: [sheet], done }]])
    const read = vi.fn()
    const sendNote = vi.fn()
    const host = { say: () => {}, openBot: () => {}, words, read, keep: vi.fn(), sendNote }
    const cutMira = { ...mira([]), state: 'failed', cuts: [{ id: 'w1@t' }] } as unknown as XrBot
    const { result } = renderHook(() => useBoards([cutMira], host, vi.fn()))
    act(() => { result.current.take('w:w1') })
    clock += BOARD.calmMs + 1
    await act(async () => { result.current.stampBox('w:w1', { u: 0.5, v: 0.5 }, 0) })
    await act(async () => { result.current.stampBox('w:w1', { u: 0.6, v: 0.5 }, 0) })
    expect(cutRetry).toHaveBeenCalledTimes(1)
    expect(cutRetry).toHaveBeenCalledWith('w1')
    act(() => { result.current.release('w:w1', { px: 380, pxPerMs: 4, lastMs: 8, onBot: false }) })
    expect(cutRetry).toHaveBeenCalledTimes(1)
    // The failed sheet that he threw names its own cut (P3 M2): never what the clipboard says by then.
    expect(read).toHaveBeenCalledWith('mira', 'fail|w1@t', 'w1', 'w1@t')
    expect(sendNote).not.toHaveBeenCalled()
    expect(result.current.probe().retried).toEqual(['w1'])
  })

  it('tells that he has looked when he takes a sheet, and only then (D39, ruling 1)', () => {
    const words = new Map([['mira', { id: 'w:w1', version: '2026-11-21T10:42:00Z|3', slot: 'w1', raise: true, state: 'ok' as const, sentence: 'Done.', sheets: [] }]])
    const looked = vi.fn()
    const host = { say: () => {}, openBot: () => {}, words, read: vi.fn(), keep: vi.fn(), sendNote: vi.fn(), looked }
    const { result } = renderHook(() => useBoards([mira([])], host, vi.fn()))
    expect(looked).not.toHaveBeenCalled()
    act(() => { result.current.take('w:w1') })
    expect(looked).toHaveBeenCalledWith('mira', 'done')
  })

  it('sends nothing when the sheet\'s cut is gone meanwhile, and says the Bot runs again already (P3 M1)', async () => {
    let clock = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    vi.mocked(cutRetry).mockClear()
    const done = { head: 'Mira: stopped · 10:42', short: 'stopped · 10:42', made: [], left: [], failed: { cut: 'w1@t' } }
    const sheet = { head: ['Not signed in.'], body: [], n: 1, of: 1, before: false, left: null, choices: ['Try again'], done }
    const words = new Map([['mira', { id: 'w:w1', version: 'fail|w1@t', slot: 'w1', raise: true, state: 'ok' as const, sentence: 'Not signed in.', sheets: [sheet], done }]])
    const said: string[] = []
    const host = { say: (w: string) => { said.push(w) }, openBot: () => {}, words, read: vi.fn(), keep: vi.fn(), sendNote: vi.fn() }
    // Her turn was picked up elsewhere and ended while he held the sheet: no cut is left.
    const resumed = { ...mira([]), state: 'idle', cuts: [] } as unknown as XrBot
    const { result } = renderHook(() => useBoards([resumed], host, vi.fn()))
    act(() => { result.current.take('w:w1') })
    clock += BOARD.calmMs + 1
    await act(async () => { result.current.stampBox('w:w1', { u: 0.5, v: 0.5 }, 0) })
    expect(cutRetry).not.toHaveBeenCalled()
    expect(said.at(-1)).toMatch(/running again already/)
  })

  it('reads a done sheet when he takes it, up to its own answer (D77 4), hands it back kept, and files it by a throw; neither sends', () => {
    vi.useFakeTimers()
    onTestFinished(() => { vi.useRealTimers() })
    const words = new Map([['mira', { id: 'w:w1', version: '2026-11-21T10:42:00Z|3', slot: 'w1', raise: true, state: 'ok' as const, sentence: 'Done.', sheets: [] }]])
    const read = vi.fn()
    const keep = vi.fn()
    const sendNote = vi.fn()
    const decide = vi.fn()
    const host = { say: () => {}, openBot: () => {}, words, read, keep, sendNote }
    const { result } = renderHook(() => useBoards([mira([])], host, decide))
    expect(result.current.probe().boards.map(b => b.needId)).toEqual(['w:w1'])
    act(() => { result.current.take('w:w1') })
    expect(read.mock.calls).toEqual([['mira', '2026-11-21T10:42:00Z|3', 'w1', undefined]])
    act(() => { result.current.release('w:w1', { px: 20, pxPerMs: 0.1, lastMs: 200, onBot: true }) })
    act(() => { vi.advanceTimersByTime(BOARD.lowerAfterMs) })
    // A done sheet names no cut: nothing of another session's failure is put aside with it (P3 M2).
    expect(keep).toHaveBeenCalledWith('mira', 'w1', undefined)
    act(() => { result.current.take('w:w1') })
    act(() => { result.current.release('w:w1', { px: 380, pxPerMs: 4, lastMs: 8, onBot: false }) })
    expect(read).toHaveBeenCalledWith('mira', '2026-11-21T10:42:00Z|3', 'w1', undefined)
    expect(sendNote).not.toHaveBeenCalled()
    expect(decide).not.toHaveBeenCalled()
  })
})

describe('his note over a reload (M45)', () => {
  it('brings a board up with the note it carried, put by, and offers it for the record again', () => {
    const n = { ...need('a-atlas'), bot: 'atlas', who: 'atlas' }
    const crew = [{ slug: 'atlas', label: 'atlas', needs: [n] }] as unknown as XrBot[]
    const host = { say: () => {}, openBot: () => {}, notesAtStart: { 'a-atlas': { note: 'Ship it after lunch', put: true } } }
    const { result } = renderHook(() => useBoards(crew, host, vi.fn()))
    const b = result.current.probe().boards.find(x => x.needId === 'a-atlas')
    expect(b).toMatchObject({ where: 'bot', comment: 'Ship it after lunch' })
    expect(result.current.keptNotes).toEqual({ 'a-atlas': { note: 'Ship it after lunch', put: true, bot: 'atlas' } })
  })
})

describe('his note when its board goes and comes back (M45)', () => {
  it('keeps the note in the book while the Bot is not drawn, and puts it on the board that comes back', () => {
    const n = { ...need('a-atlas'), bot: 'atlas', who: 'atlas' }
    const crew = [{ slug: 'atlas', label: 'atlas', needs: [n] }] as unknown as XrBot[]
    const base = { say: () => {}, openBot: () => {}, notesAtStart: { 'a-atlas': { note: 'Ship it after lunch', put: true } } }
    const { result, rerender } = renderHook(({ drawn }) => useBoards(crew, { ...base, drawn }, vi.fn()), { initialProps: { drawn: new Set(['atlas']) } })
    expect(result.current.probe().boards.find(x => x.needId === 'a-atlas')?.comment).toBe('Ship it after lunch')
    rerender({ drawn: new Set<string>() })
    expect(result.current.probe().boards).toHaveLength(0)
    expect(result.current.keptNotes).toEqual({ 'a-atlas': { note: 'Ship it after lunch', put: true, bot: 'atlas' } })
    rerender({ drawn: new Set(['atlas']) })
    expect(result.current.probe().boards.find(x => x.needId === 'a-atlas')?.comment).toBe('Ship it after lunch')
  })
})

describe('a kept note whose ask ended (M49, F2)', () => {
  const n = { ...need('a-atlas'), bot: 'atlas', who: 'atlas' }
  const withAsk = [{ slug: 'atlas', label: 'atlas', needs: [n] }] as unknown as XrBot[]
  const without = [{ slug: 'atlas', label: 'atlas', needs: [] }] as unknown as XrBot[]
  const base = { say: () => {}, openBot: () => {}, notesAtStart: { 'a-atlas': { note: 'Ship it after lunch', put: true, bot: 'atlas' } } }
  const ended = { 'a-atlas': { note: 'Ship it after lunch', put: false, bot: 'atlas', ended: true } }
  const back = { px: 20, pxPerMs: 0.1, lastMs: 200, onBot: true }
  const thrown = { px: 380, pxPerMs: 4, lastMs: 8, onBot: false }
  it('stays in the book, marked as ended, and is shown at its Bot on a sheet that says so (M55)', () => {
    const { result } = renderHook(() => useBoards(without, base, vi.fn()))
    expect(result.current.keptNotes).toEqual(ended)
    expect(result.current.probe().boards.map(b => ({ id: b.needId, where: b.where, comment: b.comment }))).toEqual([{ id: 'a-atlas', where: 'bot', comment: 'Ship it after lunch' }])
    expect(result.current.views[0].text?.sentence).toContain('answered elsewhere')
  })
  it('makes the sheet from the book again after a reload, and when its Bot is drawn again (M55)', () => {
    const { result, rerender } = renderHook(({ drawn }) => useBoards(without, { ...base, notesAtStart: ended, drawn }, vi.fn()), { initialProps: { drawn: new Set<string>() } })
    expect(result.current.probe().boards).toEqual([])
    expect(result.current.keptNotes).toEqual(ended)
    rerender({ drawn: new Set(['atlas']) })
    expect(result.current.probe().boards.map(b => b.comment)).toEqual(['Ship it after lunch'])
    rerender({ drawn: new Set<string>() })
    expect(result.current.probe().boards).toEqual([])
    expect(result.current.keptNotes).toEqual(ended)
  })
  it('leaves the book by his hand-over, which sends one fixed line and then his note (M57)', async () => {
    const sendNote = vi.fn(() => Promise.resolve(true))
    const { result } = renderHook(() => useBoards(without, { ...base, sendNote }, vi.fn()))
    act(() => { result.current.take('a-atlas') })
    await act(async () => { result.current.release('a-atlas', back) })
    expect(sendNote).toHaveBeenCalledTimes(1)
    expect(sendNote.mock.calls[0]).toEqual(['atlas', 'Written on a request that was answered elsewhere:\nShip it after lunch', 'note', noteSendId('a-atlas', 'Ship it after lunch')])
    expect(result.current.keptNotes).toEqual({})
    expect(result.current.probe().boards).toEqual([])
  })
  it('gives one note one send id, also after a reload, and another note or another ask another id (M85)', async () => {
    const ids: string[] = []
    const sendNote = vi.fn((_s: string, _t: string, _f?: string, id?: string) => { ids.push(id ?? ''); return Promise.resolve(false) })
    // He hands the note over; it does not go; he hands it over again. Then a new tab (a reload) with the same book.
    const first = renderHook(() => useBoards(without, { ...base, sendNote }, vi.fn()))
    for (let i = 0; i < 2; i += 1) { act(() => { first.result.current.take('a-atlas') }); await act(async () => { first.result.current.release('a-atlas', back) }) }
    const second = renderHook(() => useBoards(without, { ...base, sendNote }, vi.fn()))
    act(() => { second.result.current.take('a-atlas') })
    await act(async () => { second.result.current.release('a-atlas', back) })
    expect(ids).toHaveLength(3)
    expect(new Set(ids).size).toBe(1)
    expect(ids[0]).toMatch(/^n-[0-9a-f]{8}-[0-9a-f]{8}-[0-9a-f]{8}$/)
    // The other side: two different notes on one ask both go, each under its own id; so does the same note on another ask.
    expect(noteSendId('a-atlas', 'Ship it after lunch')).not.toBe(noteSendId('a-atlas', 'Ship it before lunch'))
    expect(noteSendId('a-atlas', 'Ship it after lunch')).not.toBe(noteSendId('a-juno', 'Ship it after lunch'))
  })
  it('counts a send with no answer in 20 s as not sent: the sheet is back and he is told; a late answer changes nothing (M85)', async () => {
    vi.useFakeTimers()
    try {
      let answer: (went: boolean) => void = () => {}
      const say = vi.fn()
      const sendNote = vi.fn(() => new Promise<boolean>(done => { answer = done }))
      const { result } = renderHook(() => useBoards(without, { ...base, say, sendNote }, vi.fn()))
      act(() => { result.current.take('a-atlas') })
      act(() => { result.current.release('a-atlas', back) })
      act(() => { vi.advanceTimersByTime(NOTE_SEND_WAIT_MS - 1) })
      expect(result.current.probe().boards).toEqual([])
      expect(say).not.toHaveBeenCalledWith('The note may not have gone. Write it again.')
      act(() => { vi.advanceTimersByTime(1) })
      expect(result.current.probe().boards.map(b => ({ where: b.where, comment: b.comment }))).toEqual([{ where: 'bot', comment: 'Ship it after lunch' }])
      expect(result.current.keptNotes).toEqual(ended)
      expect(say).toHaveBeenCalledWith('The note may not have gone. Write it again.')
      await act(async () => { answer(true) })
      expect(result.current.probe().boards.map(b => b.where)).toEqual(['bot'])
      expect(result.current.keptNotes).toEqual(ended)
    } finally { vi.useRealTimers() }
  })
  it('is at its Bot and in the book again when the hand-over did not go', async () => {
    const sendNote = vi.fn(() => Promise.resolve(false))
    const { result } = renderHook(() => useBoards(without, { ...base, sendNote }, vi.fn()))
    act(() => { result.current.take('a-atlas') })
    await act(async () => { result.current.release('a-atlas', back) })
    expect(result.current.probe().boards.map(b => b.where)).toEqual(['bot'])
    expect(result.current.keptNotes).toEqual(ended)
  })
  it('stays in the book while the send is under way, with no sheet, and leaves it only when the send says it went (M64)', async () => {
    let answer: (went: boolean) => void = () => {}
    const sendNote = vi.fn(() => new Promise<boolean>(done => { answer = done }))
    const { result } = renderHook(() => useBoards(without, { ...base, sendNote }, vi.fn()))
    act(() => { result.current.take('a-atlas') })
    act(() => { result.current.release('a-atlas', back) })
    expect(result.current.keptNotes).toEqual(ended)
    expect(result.current.probe().boards).toEqual([])
    await act(async () => { answer(true) })
    expect(result.current.keptNotes).toEqual({})
    expect(result.current.probe().boards).toEqual([])
    expect(sendNote).toHaveBeenCalledTimes(1)
  })
  it.each([
    ['there is no send', undefined],
    ['the send gives nothing', () => undefined],
    ['the send throws', () => Promise.reject(new Error('down'))],
  ])('keeps the note and the sheet, and says so, when %s (M64)', async (_name, sendNote) => {
    const say = vi.fn()
    const { result } = renderHook(() => useBoards(without, { ...base, say, sendNote }, vi.fn()))
    act(() => { result.current.take('a-atlas') })
    await act(async () => { result.current.release('a-atlas', back) })
    expect(result.current.probe().boards.map(b => ({ where: b.where, comment: b.comment }))).toEqual([{ where: 'bot', comment: 'Ship it after lunch' }])
    expect(result.current.keptNotes).toEqual(ended)
    expect(say).toHaveBeenCalledWith('The note may not have gone. Write it again.')
  })
  it('leaves the book by his throw, and nothing is sent', () => {
    const sendNote = vi.fn(() => Promise.resolve(true))
    const { result } = renderHook(() => useBoards(without, { ...base, sendNote }, vi.fn()))
    act(() => { result.current.take('a-atlas') })
    act(() => { result.current.release('a-atlas', thrown) })
    expect(sendNote).not.toHaveBeenCalled()
    expect(result.current.keptNotes).toEqual({})
    expect(result.current.probe().boards).toEqual([])
  })
  it('is not its Bot\'s one board: the next ask shows its board and the sheet waits behind it (M56)', () => {
    const next = [{ slug: 'atlas', label: 'atlas', needs: [{ ...need('a-next'), bot: 'atlas', who: 'atlas' }] }] as unknown as XrBot[]
    const { result, rerender } = renderHook(({ crew }) => useBoards(crew, base, vi.fn()), { initialProps: { crew: without } })
    rerender({ crew: next })
    expect(result.current.probe().boards.map(b => b.needId).sort()).toEqual(['a-atlas', 'a-next'])
    expect(result.current.views.map(v => v.needId)).toEqual(['a-next'])
    expect(result.current.keptNotes).toEqual(ended)
    rerender({ crew: without })
    expect(result.current.views.map(v => v.needId)).toEqual(['a-atlas'])
  })
  it('gives an ask that comes again with that id no old note', () => {
    const { result, rerender } = renderHook(({ crew }) => useBoards(crew, { ...base, notesAtStart: { 'a-atlas': { note: 'old', put: false } } }, vi.fn()), { initialProps: { crew: without } })
    expect(result.current.keptNotes).toEqual({ 'a-atlas': { note: 'old', put: false, ended: true } })
    rerender({ crew: withAsk })
    expect(result.current.probe().boards.find(b => b.needId === 'a-atlas')?.comment).toBe('')
    expect(result.current.keptNotes).toEqual({ 'a-atlas': { note: 'old', put: false, ended: true } })
  })
  it('keeps the book while the crew is not read whole', () => {
    const { result } = renderHook(() => useBoards(without, { ...base, known: false }, vi.fn()))
    expect(result.current.keptNotes).toEqual({ 'a-atlas': { note: 'Ship it after lunch', put: true, bot: 'atlas' } })
  })
  it('refuses a note past the record\'s length at the keys', () => {
    const say = vi.fn()
    const { result } = renderHook(() => useBoards(withAsk, { say, openBot: () => {} }, vi.fn()))
    act(() => { result.current.take('a-atlas') })
    act(() => { result.current.note('a-atlas', 'x'.repeat(1000)) })
    act(() => { result.current.note('a-atlas', 'x'.repeat(1001)) })
    expect(result.current.probe().boards[0].comment).toHaveLength(1000)
    expect(say).toHaveBeenCalledTimes(1)
  })
})

describe('a third take (D79 2)', () => {
  const ask = (id: string, bot: string): XrNeed => ({ ...need(id), bot, who: bot })
  const crewOf = (bots: string[]) => bots.map(slug => ({ slug, label: slug, needs: [ask(`a-${slug}`, slug)] }) as unknown as XrBot)
  const start = () => {
    let clock = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => (clock += 10))
    const later = (ms: number) => { clock += ms }
    const decide = vi.fn(() => Promise.resolve('approved' as const))
    const sendNote = vi.fn(() => Promise.resolve(true))
    const host = { say: () => {}, openBot: () => {}, sendNote }
    const hook = renderHook(() => useBoards(crewOf(['atlas', 'juno', 'nova']), host, decide))
    return { ...hook, decide, sendNote, later, where: (id: string) => hook.result.current.probe().boards.find(b => b.needId === id) }
  }
  it('puts the oldest board back as it is, its finished note kept, and sends nothing', () => {
    const { result, decide, sendNote, where } = start()
    act(() => { result.current.take('a-atlas') })
    act(() => { result.current.foot('a-atlas') })
    act(() => { result.current.note('a-atlas', 'after lunch') })
    act(() => { result.current.foot('a-atlas') })
    act(() => { result.current.take('a-juno') })
    act(() => { result.current.take('a-nova') })
    expect(where('a-atlas')).toMatchObject({ where: 'bot', comment: 'after lunch' })
    expect(where('a-juno')?.where).toBe('held')
    expect(where('a-nova')?.where).toBe('held')
    expect(sendNote).not.toHaveBeenCalled()
    expect(decide).not.toHaveBeenCalled()
  })
  it('files a stamped board that a third take puts back, as a throw does: its finished note goes to its Bot once (D83 1)', async () => {
    const { result, sendNote, where, later } = start()
    act(() => { result.current.take('a-atlas') })
    later(BOARD.calmMs + 1)
    await act(async () => { result.current.stamp('a-atlas', { u: 0.5, v: 0.5 }) })
    act(() => { result.current.foot('a-atlas') })
    act(() => { result.current.note('a-atlas', 'after lunch') })
    act(() => { result.current.foot('a-atlas') })
    act(() => { result.current.take('a-juno') })
    act(() => { result.current.take('a-nova') })
    expect(where('a-atlas')?.where).not.toBe('held')
    expect(sendNote.mock.calls.map(c => c.slice(0, 2))).toEqual([['atlas', 'after lunch']])
  })
  it('never puts back the board he writes on: the next oldest goes, and his half note stays on his', () => {
    const { result, sendNote, where } = start()
    act(() => { result.current.take('a-atlas') })
    act(() => { result.current.foot('a-atlas') })
    act(() => { result.current.note('a-atlas', 'half a') })
    act(() => { result.current.take('a-juno') })
    act(() => { result.current.take('a-nova') })
    expect(where('a-atlas')).toMatchObject({ where: 'held', writing: true, comment: 'half a' })
    expect(where('a-juno')?.where).toBe('bot')
    expect(sendNote).not.toHaveBeenCalled()
  })
  it('writes on one board at a time, so the board he opened last stays and the other goes as it is', () => {
    const { result, sendNote, where } = start()
    act(() => { result.current.take('a-atlas') })
    act(() => { result.current.foot('a-atlas') })
    act(() => { result.current.note('a-atlas', 'first') })
    act(() => { result.current.take('a-juno') })
    // A pinch on the second board's foot ends the line on the first (`foot`): one line is open at a time.
    act(() => { result.current.foot('a-juno') })
    act(() => { result.current.take('a-nova') })
    expect(where('a-atlas')).toMatchObject({ where: 'bot', comment: 'first' })
    expect(where('a-juno')).toMatchObject({ where: 'held', writing: true })
    expect(sendNote).not.toHaveBeenCalled()
  })
})

// D192 (15:50): a cancel that is not his let-go (a lost hand, a hand that unmounts, a hidden session) acts on nothing.
describe('a held board whose press is ended by a cancel that is not his (D192)', () => {
  const fast = { handM: 0.2, handMps: 1.5 }
  it('must not act: a kept note on an ask answered elsewhere stays on the board, nothing is sent', () => {
    const without = [{ slug: 'atlas', label: 'atlas', needs: [] }] as unknown as XrBot[]
    const sendNote = vi.fn(() => Promise.resolve(true))
    const base = { say: () => {}, openBot: () => {}, sendNote, notesAtStart: { 'a-atlas': { note: 'Ship it after lunch', put: true, bot: 'atlas' } } }
    const { result } = renderHook(() => useBoards(without, base, vi.fn()))
    act(() => { result.current.take('a-atlas') })
    act(() => { result.current.release('a-atlas', cancelMoved(false, fast)) })
    expect(sendNote).not.toHaveBeenCalled()
    expect(result.current.probe().boards.map(b => ({ where: b.where, comment: b.comment }))).toEqual([{ where: 'held', comment: 'Ship it after lunch' }])
    expect(result.current.keptNotes['a-atlas']?.note).toBe('Ship it after lunch')
  })
  it('must not act: an unmarked board is not thrown to the floor; must not lose: his fast up still throws it', () => {
    const decide = vi.fn(() => Promise.resolve('approved' as const))
    const { result } = renderHook(() => useBoards([mira([need('a')])], { say: () => {}, openBot: () => {} }, decide))
    act(() => { result.current.take('a') })
    act(() => { result.current.release('a', STAY_MOVED) })
    expect(result.current.probe().boards.map(b => b.where)).toEqual(['held'])
    act(() => { result.current.release('a', cancelMoved(true, fast)) })
    expect(result.current.probe().boards.map(b => b.where)).not.toEqual(['held'])
    expect(decide).not.toHaveBeenCalled()
  })
  it('must not lose: a board kept through such a cancel is taken again, and his slow let-go hands it back', () => {
    const { result } = renderHook(() => useBoards([mira([need('a')])], { say: () => {}, openBot: () => {} }, vi.fn()))
    act(() => { result.current.take('a') })
    act(() => { result.current.release('a', STAY_MOVED) })
    act(() => { result.current.release('a', { handM: 0.05, handMps: 0.2 }) })
    expect(result.current.probe().boards.map(b => b.where)).toEqual(['bot'])
    act(() => { result.current.take('a') })
    expect(result.current.probe().boards.map(b => b.where)).toEqual(['held'])
  })
})
