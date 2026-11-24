import { describe, expect, it } from 'vitest'
import { SINCE_LINE, changeOf, dealt, enter, lookedAt, otherDay, parseSeen, parseSince, regroup, seenNow, sinceCounts, sinceCuts, sinceLinePitch, sinceLit, stands, stillSince, type Seen, type Since, type SinceBot } from './since'

/** The visit: 14:05 of a day, in ms; a Bot's `lastTs` is in seconds. */
const LEFT = Date.UTC(2026, 10, 21, 14, 5)
const after = (min: number) => Math.floor((LEFT + min * 60_000) / 1000)
const bot = (slug: string, over: Partial<SinceBot> = {}): SinceBot => ({ slug, state: 'idle', lastTs: after(-30), ownUnread: false, needs: [], cuts: [], ...over })
const cut = (id: string, over: Partial<SinceBot['cuts'][number]> = {}) => ({ id, stale: false, ...over })
const crewThen = [bot('mira', { state: 'working' }), bot('juno', { needs: [{ id: 'a1' }] }), bot('pico', { cuts: [cut('c0')] }), bot('otto'), bot('sage', { state: 'working' })]
const seen: Seen = seenNow(crewThen, LEFT)
/** The crew as he finds it: Mira finished with words, Juno asks anew, Pico was cut anew, Otto as before, Sage still works. */
const crewNow = [
  bot('mira', { lastTs: after(20), ownUnread: true }),
  bot('juno', { state: 'needs', needs: [{ id: 'a1' }, { id: 'a2' }] }),
  bot('pico', { state: 'failed', cuts: [cut('c0'), cut('c1')] }),
  bot('otto'),
  bot('sage', { state: 'working', lastTs: after(25) }),
]
const lit = (s: Since | null) => sinceLit(s).sort()
const counted = (s: Since | null) => Object.values(sinceCounts(s)).reduce((a, b) => a + b, 0)

describe('what changed since his last visit', () => {
  it('keeps of a visit when it was and each Bot\'s asks and cuts', () => {
    expect(seen.at).toBe(LEFT)
    expect(seen.bots.juno).toEqual({ asks: ['a1'], cuts: [], ts: after(-30) })
    expect(seen.bots.pico).toEqual({ asks: [], cuts: ['c0'], ts: after(-30) })
  })
  it('puts a Bot in one group: it needs him, it failed, or it finished with words he has not read', () => {
    const since = enter(null, seen, crewNow)
    expect(since?.at).toBe(LEFT)
    expect(since?.bots).toEqual({ mira: { group: 'finished', ids: [] }, juno: { group: 'needs', ids: ['a2'] }, pico: { group: 'failed', ids: ['c1'] } })
    expect(sinceCounts(since)).toEqual({ needs: 1, failed: 1, finished: 1 })
  })
  it('names the new cuts, which the rim draws red, for as long as he has not dealt with them', () => {
    const since = enter(null, seen, crewNow)
    // A cut needs no unread words: the rim's own state (idle, with the dim mark) is a failed Bot here too.
    expect(sinceCuts(since)).toEqual(['c1'])
    expect(changeOf(bot('pico', { state: 'idle', cuts: [cut('c0'), cut('c1')] }), seen)).toEqual({ group: 'failed', ids: ['c1'] })
    expect(sinceCuts(dealt(since, 'pico'))).toEqual([])
    expect(sinceCuts(null)).toEqual([])
  })
  it('lights exactly the Bots that the line counts', () => {
    const since = enter(null, seen, crewNow)
    expect(lit(since)).toEqual(['juno', 'mira', 'pico'])
    expect(counted(since)).toBe(lit(since).length)
    // And so after each thing he deals with.
    let s = since
    for (const slug of ['mira', 'pico', 'juno']) { s = dealt(s, slug); expect(counted(s)).toBe(lit(s).length) }
    expect(s).toBeNull()
  })
  it('counts the most pressing group only: a Bot that finished and then asked needs him', () => {
    const both = bot('mira', { state: 'needs', lastTs: after(20), ownUnread: true, needs: [{ id: 'a9' }] })
    // It keeps that it was active since: when the ask ends it falls to finished (P4 M3).
    expect(changeOf(both, seen)).toEqual({ group: 'needs', ids: ['a9'], active: true })
    const cutToo = bot('pico', { state: 'failed', lastTs: after(20), ownUnread: true, cuts: [cut('c1')] })
    expect(changeOf(cutToo, seen)?.group).toBe('failed')
  })
  it('counts nothing that is no change', () => {
    // It worked and came to rest with nothing unread (he read it on his phone).
    expect(changeOf(bot('mira', { lastTs: after(20) }), seen)).toBeNull()
    // Unread words from before the visit: he saw them there.
    expect(changeOf(bot('otto', { ownUnread: true, lastTs: after(-30) }), seen)).toBeNull()
    // It finished while he was IN the room, a moment before he left: the record followed it, so it is not "since".
    const inRoom = bot('otto', { ownUnread: true, lastTs: after(-0.01) })
    expect(changeOf(inRoom, seenNow([inRoom], LEFT))).toBeNull()
    // And it is, when it speaks again after he left.
    expect(changeOf({ ...inRoom, lastTs: after(3) }, seenNow([inRoom], LEFT))?.group).toBe('finished')
    // It still works.
    expect(changeOf(bot('sage', { state: 'working', lastTs: after(25), ownUnread: true }), seen)).toBeNull()
    // The ask and the cut that it had then; a cut that is old; a cut that he has read.
    expect(changeOf(bot('juno', { state: 'needs', needs: [{ id: 'a1' }] }), seen)).toBeNull()
    expect(changeOf(bot('pico', { state: 'failed', cuts: [cut('c0')] }), seen)).toBeNull()
    expect(changeOf(bot('pico', { state: 'failed', cuts: [cut('c2', { stale: true })] }), seen)).toBeNull()
    // A Bot that is new to the crew; a Bot that left it.
    expect(changeOf(bot('nova', { state: 'needs', needs: [{ id: 'n1' }] }), seen)).toBeNull()
    expect(enter(null, seen, crewNow.filter(b => b.slug !== 'mira'))?.bots.mira).toBeUndefined()
    // An ask that came and was answered while he was away.
    expect(changeOf(bot('otto'), seen)).toBeNull()
  })
  it('shows no line in a browser that kept no visit, and none when nothing changed', () => {
    expect(enter(null, null, crewNow)).toBeNull()
    expect(enter(null, seen, crewThen)).toBeNull()
  })
  it('clears each one as he deals with it, here or on another device', () => {
    const since = enter(null, seen, crewNow)
    // Mira's words are read (anywhere): the gateway's read state says so.
    const read = crewNow.map(b => (b.slug === 'mira' ? { ...b, ownUnread: false } : b))
    expect(lit(stillSince(since, read))).toEqual(['juno', 'pico'])
    // Juno's new ask is answered; its old one still waits: it is not "since" any more.
    const answered = read.map(b => (b.slug === 'juno' ? { ...b, needs: [{ id: 'a1' }] } : b))
    expect(lit(stillSince(since, answered))).toEqual(['pico'])
    // Pico's thread runs again: the cut is gone. (A tap on Pico clears it here: `dealt`.)
    expect(stillSince(since, answered.map(b => (b.slug === 'pico' ? { ...b, state: 'working' as const, cuts: [cut('c0')] } : b)))).toBeNull()
    expect(lit(dealt(stillSince(since, answered), 'pico'))).toEqual([])
    // Nothing changed: the same object, so nothing is drawn anew.
    expect(stillSince(since, crewNow)).toBe(since)
    // A Bot that left the crew is not lit.
    expect(lit(stillSince(since, crewNow.filter(b => b.slug !== 'juno')))).toEqual(['mira', 'pico'])
  })
  it('keeps over a reload what he has not read, and adds what changed since', () => {
    const first = enter(null, seen, crewNow)
    // He read nothing and reloads ten minutes later: the visit that was kept is ten minutes old now.
    const later = seenNow(crewNow, LEFT + 30 * 60_000)
    const again = enter(first, later, crewNow)
    expect(again?.at).toBe(LEFT)
    expect(lit(again)).toEqual(['juno', 'mira', 'pico'])
    // Otto finishes while the page is closed: it joins them. Mira, read meanwhile, is gone.
    const next = crewNow.map(b => (b.slug === 'otto' ? { ...b, lastTs: after(40), ownUnread: true } : b.slug === 'mira' ? { ...b, ownUnread: false } : b))
    expect(lit(enter(first, later, next))).toEqual(['juno', 'otto', 'pico'])
    // A Bot in both keeps the more pressing group.
    const asks = crewNow.map(b => (b.slug === 'mira' ? { ...b, state: 'needs' as const, needs: [{ id: 'm1' }] } : b))
    expect(enter(first, later, asks)?.bots.mira).toEqual({ group: 'needs', ids: ['m1'] })
    // With nothing left of the entry before, the time is the newer visit's.
    const fresh = enter(null, later, next)
    expect(fresh?.at).toBe(later.at)
  })
  it('names the day when the visit was on another day', () => {
    const noon = new Date(2026, 10, 21, 12, 0).getTime()
    expect(otherDay(new Date(2026, 10, 21, 8, 0).getTime(), noon)).toBe(false)
    expect(otherDay(new Date(2026, 10, 20, 18, 20).getTime(), noon)).toBe(true)
  })
  it('stands above Home\'s strip, and under it when above would be past what the glasses show', () => {
    // Home's strip with places: 16 degrees up, 8.5 cm high, 1.75 m away.
    const above = sinceLinePitch(16, 0.085, 1.75)
    expect(above).toBeGreaterThan(16)
    expect(above + (Math.atan2(SINCE_LINE.heightM / 2, 1.75) * 180) / Math.PI).toBeLessThanOrEqual(SINCE_LINE.mostPitchDeg)
    // The strip above a page's top, 19 degrees up: the line would leave the view, so it stands under the strip.
    expect(sinceLinePitch(19, 0.085, 1.75)).toBeLessThan(19)
  })
  it('reads a record that is whole, and drops what is none', () => {
    const since = enter(null, seen, crewNow)
    expect(parseSeen(JSON.parse(JSON.stringify(seen)))).toEqual(seen)
    expect(parseSince(JSON.parse(JSON.stringify(since)))).toEqual(since)
    expect(parseSeen(null)).toBeNull()
    expect(parseSeen({ at: 'now', bots: {} })).toBeNull()
    expect(parseSeen({ at: LEFT, bots: { a: { asks: ['x'], cuts: [], ts: 5 }, b: { asks: [7], cuts: [], ts: 5 }, c: { asks: [], cuts: [] }, '': { asks: [], cuts: [], ts: 5 } } })).toEqual({ at: LEFT, bots: { a: { asks: ['x'], cuts: [], ts: 5 } } })
    expect(parseSince({ at: LEFT, bots: { a: { group: 'dancing', ids: [] } } })).toBeNull()
    expect(parseSince({ at: LEFT, bots: { a: { group: 'finished', ids: [] }, b: { group: 'needs' } } })).toEqual({ at: LEFT, bots: { a: { group: 'finished', ids: [] } } })
  })
})

describe('a failure he has dealt with (REVIEW-15 P4 M2)', () => {
  const item = { group: 'failed' as const, ids: ['c1'] }
  it('stands while its cut is there, is not old, and the rim has not seen it', () => {
    expect(stands(item, bot('pico', { cuts: [cut('c1')] }))).toBe(true)
    // A press on a panel of its page marks the cut seen: the rim dims, Home says 0 failed, and the line drops it.
    expect(stands(item, bot('pico', { cuts: [cut('c1')] }), new Set(['c1']))).toBe(false)
    // The cut turns old: the rim dims, and the line drops it too.
    expect(stands(item, bot('pico', { cuts: [cut('c1', { stale: true })] }))).toBe(false)
    const since = enter(null, seen, crewNow)
    expect(lit(stillSince(since, crewNow, new Set(['c1'])))).toEqual(['juno', 'mira'])
  })
})

describe('a Bot that failed and also asks (REVIEW-15 P4 M3)', () => {
  // Juno asks anew on its own thread and a worker of it was cut anew; it was active since, with words he has not read.
  const juno = bot('juno', { state: 'needs', lastTs: after(20), ownUnread: true, needs: [{ id: 'a1' }, { id: 'a2' }], cuts: [cut('w1')] })
  it('needs him first, and keeps the new cut: the rim draws it red meanwhile', () => {
    const item = changeOf(juno, seen)
    expect(item).toEqual({ group: 'needs', ids: ['a2'], cuts: ['w1'], active: true })
    expect(sinceCuts(enter(null, seen, [juno]))).toEqual(['w1'])
  })
  it('falls to failed when the ask ends, then to finished when the cut is dealt with, then goes', () => {
    const item = changeOf(juno, seen)!
    const answered = { ...juno, state: 'failed' as const, needs: [{ id: 'a1' }] }
    const failed = regroup(item, answered)
    expect(failed).toEqual({ group: 'failed', ids: ['w1'], active: true })
    const resumed = { ...answered, state: 'idle' as const }
    expect(regroup(failed!, resumed, new Set(['w1']))).toEqual({ group: 'finished', ids: [] })
    expect(regroup({ group: 'finished', ids: [] }, { ...resumed, ownUnread: false })).toBeNull()
    // The line follows, step by step.
    const since = enter(null, seen, [juno])
    expect(sinceCounts(stillSince(since, [answered]))).toEqual({ needs: 0, failed: 1, finished: 0 })
    expect(sinceCounts(stillSince(stillSince(since, [answered]), [resumed], new Set(['w1'])))).toEqual({ needs: 0, failed: 0, finished: 1 })
  })
  it('keeps what it carries over a reload', () => {
    expect(parseSince({ at: LEFT, bots: { juno: { group: 'needs', ids: ['a2'], cuts: ['w1'], active: true } } })?.bots.juno).toEqual({ group: 'needs', ids: ['a2'], cuts: ['w1'], active: true })
  })
})

describe('a take clears only what he took (D39.1, made exact)', () => {
  const juno = bot('juno', { state: 'needs', lastTs: after(20), ownUnread: true, needs: [{ id: 'a1' }, { id: 'a2' }], cuts: [cut('w1')] })
  it('a Bot that asks and failed, its failed sheet taken: "1 need you" stays and it stays lit; "failed" goes', () => {
    const since = enter(null, seen, [juno])
    const taken = lookedAt(since, 'juno', 'failed')
    expect(sinceCounts(taken)).toEqual({ needs: 1, failed: 0, finished: 0 })
    expect(lit(taken)).toEqual(['juno'])
    expect(sinceCuts(taken)).toEqual([])
    // When the ask ends, nothing of the failure comes back; that it was active does.
    expect(stillSince(taken, [{ ...juno, state: 'idle', needs: [{ id: 'a1' }] }])?.bots.juno).toEqual({ group: 'finished', ids: [] })
  })
  it('a done sheet taken drops that it finished, and keeps an ask and a failure', () => {
    const since = enter(null, seen, [juno])
    expect(lookedAt(since, 'juno', 'done')?.bots.juno).toEqual({ group: 'needs', ids: ['a2'], cuts: ['w1'] })
    const done = enter(null, seen, [bot('mira', { lastTs: after(20), ownUnread: true })])
    expect(lookedAt(done, 'mira', 'done')).toBeNull()
    const failed = enter(null, seen, [bot('pico', { state: 'failed', cuts: [cut('c0'), cut('c1')] })])
    expect(lookedAt(failed, 'pico', 'failed')).toBeNull()
  })
})
