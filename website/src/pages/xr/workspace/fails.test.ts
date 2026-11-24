import { describe, expect, it } from 'vitest'
import { FEEDS_OK, buildXrCrew, type XrBot, type XrCrew, type XrFeeds } from '../model'
import type { MemberRosterRow } from '../../../api/client'
import type { ChatSlot } from '../../../types'
import { FAILS, failDone, failGone, failQuiet, failUp, newFailBook, nextFail, readFails, type FailMoment } from './fails'

const bot = (slug: string, cut = ''): XrBot => ({ slug, name: slug, label: slug, needs: [], cut } as unknown as XrBot)
const crew = (bots: XrBot[], feeds: XrFeeds = FEEDS_OK): XrCrew => ({ bots, needs: [], unowned: 0, feeds })
const calm: FailMoment = { now: 100_000, open: true, calmSince: -100_000, calmMs: 2000, asks: () => false, standing: () => null, panelInView: false, askWaits: false, pages: 1, most: 12 }

describe('which cut brings a page', () => {
  it('brings none for what was cut when the page opened', () => {
    const book = newFailBook()
    readFails(book, crew([bot('mira', 'm#g:3')]), 0)
    expect(book.waiting.size).toBe(0)
    expect(nextFail(book, calm)).toEqual({ do: 'nothing' })
  })

  it('brings one when the Bot\'s own thread is cut in a read while he is in, once for that turn', () => {
    const book = newFailBook()
    readFails(book, crew([bot('mira'), bot('juno')]), 0)
    readFails(book, crew([bot('mira', 'm#g:3'), bot('juno')]), 1000)
    expect(nextFail(book, calm)).toEqual({ do: 'up', bot: 'mira' })
    failDone(book, 'mira')
    // The same cut again, or after a read that lost it for a moment, brings none.
    readFails(book, crew([bot('mira', 'm#g:3'), bot('juno')]), 2000)
    readFails(book, crew([bot('mira'), bot('juno')]), 3000)
    readFails(book, crew([bot('mira', 'm#g:3'), bot('juno')]), 4000)
    expect(nextFail(book, calm)).toEqual({ do: 'nothing' })
    // A later turn that is cut again is a new cut.
    readFails(book, crew([bot('mira', 'm#g:5'), bot('juno')]), 9000)
    expect(nextFail(book, calm)).toEqual({ do: 'up', bot: 'mira' })
  })

  it('brings a cut read alone up at once, with no wait', () => {
    const book = newFailBook()
    readFails(book, crew([bot('mira')]), 0)
    readFails(book, crew([bot('mira', 'm#g:3')]), 1000)
    expect(nextFail(book, { ...calm, now: 1001 })).toEqual({ do: 'up', bot: 'mira' })
  })

  it('forgets a cut that is gone before its moment', () => {
    const book = newFailBook()
    readFails(book, crew([bot('mira')]), 0)
    readFails(book, crew([bot('mira', 'm#g:3')]), 1000)
    readFails(book, crew([bot('mira')]), 1500)
    expect(nextFail(book, calm)).toEqual({ do: 'nothing' })
  })

  it('reads the first whole read after the live connection came back as the page\'s opening: rims only', () => {
    const book = newFailBook()
    readFails(book, crew([bot('mira')]), 0)
    readFails(book, crew([bot('mira')], { ...FEEDS_OK, live: false }), 1000)
    readFails(book, crew([bot('mira', 'm#h:1')]), 2000)
    expect(nextFail(book, calm)).toEqual({ do: 'nothing' })
    // From then on a cut is new again.
    readFails(book, crew([bot('mira', 'm#h:4')]), 9000)
    expect(nextFail(book, calm)).toEqual({ do: 'up', bot: 'mira' })
  })

  it('takes nothing from a read that is not whole', () => {
    const book = newFailBook()
    readFails(book, crew([bot('mira')]), 0)
    readFails(book, crew([bot('mira', 'm#g:3')], { ...FEEDS_OK, asks: 'loading' }), 1000)
    expect(book.waiting.size).toBe(0)
  })

  it('brings none when two Bots or more are cut in one read', () => {
    const book = newFailBook()
    readFails(book, crew([bot('mira'), bot('juno')]), 0)
    readFails(book, crew([bot('mira', 'm#g:3'), bot('juno', 'j#g:2')]), 1000)
    expect(nextFail(book, calm)).toEqual({ do: 'nothing' })
  })

  it('brings one page at most when the cuts are read apart: the first stays, another within five seconds keeps its rim', () => {
    const book = newFailBook()
    readFails(book, crew([bot('mira'), bot('juno'), bot('otto')]), 0)
    readFails(book, crew([bot('mira', 'm#g:3'), bot('juno'), bot('otto')]), 1000)
    expect(nextFail(book, { ...calm, now: 1200 })).toEqual({ do: 'up', bot: 'mira' })
    failUp(book, 'mira')
    readFails(book, crew([bot('mira', 'm#g:3'), bot('juno', 'j#g:2'), bot('otto')]), 1000 + FAILS.massMs - 1)
    expect(book.waiting.size).toBe(0)
    // Five seconds after the first cut, another is its own.
    readFails(book, crew([bot('mira', 'm#g:3'), bot('juno', 'j#g:2'), bot('otto', 'o#g:7')]), 1000 + FAILS.massMs)
    expect(nextFail(book, { ...calm, now: 1000 + FAILS.massMs })).toEqual({ do: 'up', bot: 'otto' })
  })

  it('keeps the rim of another Bot\'s cut that is read while one cut\'s page waits', () => {
    const book = newFailBook()
    readFails(book, crew([bot('mira'), bot('juno')]), 0)
    readFails(book, crew([bot('mira', 'm#g:3'), bot('juno')]), 1000)
    readFails(book, crew([bot('mira', 'm#g:3'), bot('juno', 'j#g:2')]), 1500)
    expect([...book.waiting.keys()]).toEqual(['mira'])
  })
})

describe('what the cut that waits does now', () => {
  const waiting = () => { const b = newFailBook(); readFails(b, crew([bot('mira')]), 0); readFails(b, crew([bot('mira', 'm#g:3')]), 1000); return b }
  it('brings none while the Bot asks: the ask\'s panel is that page', () => {
    const book = waiting()
    expect(nextFail(book, { ...calm, asks: s => s === 'mira' })).toEqual({ do: 'nothing' })
    expect(book.waiting.size).toBe(0)
  })
  it('lights the frame of the Bot\'s page that stands, once', () => {
    const book = waiting()
    expect(nextFail(book, { ...calm, standing: () => 'open-2' })).toEqual({ do: 'light', bot: 'mira', id: 'open-2' })
    expect(nextFail(book, { ...calm, standing: () => 'open-2' })).toEqual({ do: 'nothing' })
  })
  it('waits while an ask waits, while a panel stands in view, and while he is busy or not calm long enough', () => {
    for (const at of [{ ...calm, askWaits: true }, { ...calm, panelInView: true }, { ...calm, calmSince: null }, { ...calm, calmSince: calm.now - 1000 }]) {
      const book = waiting()
      expect(nextFail(book, at)).toEqual({ do: 'nothing' })
      expect(book.waiting.size).toBe(1)
    }
  })
  it('brings none for ten minutes after he kept or sent away its Bot\'s failure page', () => {
    const book = waiting()
    failQuiet(book, 'mira', calm.now - 1000)
    expect(nextFail(book, calm)).toEqual({ do: 'nothing' })
    const after = waiting()
    failQuiet(after, 'mira', calm.now - FAILS.quietMs)
    expect(nextFail(after, calm)).toEqual({ do: 'up', bot: 'mira' })
  })
  it('brings none in a full room, and none while the panels are not open', () => {
    expect(nextFail(waiting(), { ...calm, pages: 12 })).toEqual({ do: 'nothing' })
    expect(nextFail(waiting(), { ...calm, open: false })).toEqual({ do: 'nothing' })
  })
})

describe('a failure\'s page after the live connection comes back', () => {
  it('stays when its Bot\'s cut has the same key: a short drop closes nothing', () => {
    expect(failGone('member-mira#g1:3', 'member-mira#g1:3')).toBe('stays')
  })
  it('closes with the restart\'s line when the cut is of another gateway process', () => {
    expect(failGone('member-mira#g1:3', 'member-mira#g2:0')).toBe('restart')
  })
  it('closes with its own line when its turn ran again', () => {
    expect(failGone('member-mira#g1:3', '')).toBe('ran')
    expect(failGone('member-mira#g1:3', 'member-mira#g1:5')).toBe('ran')
  })
})

describe('a Bot\'s cut, as the model folds it', () => {
  const member = { slug: 'mira', name: 'Mira', slot_key: 'member-mira' } as MemberRosterRow
  const slot = (o: Partial<ChatSlot>): ChatSlot => ({ key: 'member-mira', messages: 2, running: false, mode: 'member', ...o } as ChatSlot)
  const fold = (slots: ChatSlot[]) => buildXrCrew({ members: [member], slots, approvals: [], questions: [], unread: [] }).bots[0]
  it('names its own thread\'s cut turn from `interrupted` alone, also while a sub-agent of it runs', () => {
    expect(fold([slot({ interrupted: true, turn: 4, turn_gen: 'g' })]).cut).toBe('member-mira#g:4')
    const withSub = fold([slot({ interrupted: true, subagents_running: true, turn: 4, turn_gen: 'g' })])
    expect(withSub.state).toBe('working')
    expect(withSub.cut).toBe('member-mira#g:4')
    expect(fold([slot({})]).cut).toBe('')
  })
  it('is not a worker\'s cut: that one makes the Bot failed, and brings no page', () => {
    const b = fold([slot({}), { key: 'chat-1', messages: 2, running: false, mate_root: 'member-mira', interrupted: true } as ChatSlot])
    expect(b.state).toBe('failed')
    expect(b.cut).toBe('')
  })
})
