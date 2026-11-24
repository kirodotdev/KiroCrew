import { describe, expect, it } from 'vitest'
import { NUMBERS_MOST_CHARS, NumbersLog, numbersText, sendLine, sendToOf } from './sendNumbers'

const at = new Date(Date.UTC(2026, 10, 23, 9, 30, 0))

describe('the readout\'s numbers as one message', () => {
  it('says what it is, when and on what browser, then every line', () => {
    const text = numbersText(['a · 1', 'b · 2'], at, 'Quest Browser')
    expect(text.split('\n')).toEqual(['XR readout 2026-11-23T09:30:00.000Z Quest Browser', 'a · 1', 'b · 2'])
  })

  it('cuts whole lines past the most a message may hold, and says how many', () => {
    const lines = Array.from({ length: 10 }, (_, i) => `${i}`.repeat(30))
    const text = numbersText(lines, at, 'x', 150)
    expect(text.length).toBeLessThanOrEqual(150)
    const out = text.split('\n')
    expect(out.at(-1)).toMatch(/^\[\d+ lines cut at 150 characters\]$/)
    // Each line that went is whole, and the count is of the ones that did not.
    const kept = out.slice(1, -1)
    expect(kept.every((l, i) => l === lines[i])).toBe(true)
    expect(out.at(-1)).toContain(`[${10 - kept.length} lines`)
    // The real limit is the gateway's own, far over any readout.
    expect(numbersText(lines, at, 'x')).not.toContain('cut')
    expect(NUMBERS_MOST_CHARS).toBeGreaterThan(10_000)
  })

  it('logs each kind as it changes, the hands slowly, and the pace only while a pinch is held, over a load too', () => {
    const log = new NumbersLog()
    const pace = '/chat · aims   30/s · got  0.0/s'
    const at = (s: number) => 1_000_000 + s * 1000
    log.take([pace, 'pinch · none yet', 'hands · left seen 30 cm', 'room 60 frames/s'], 1, at(0))
    log.take([pace, 'pinch · right · select · click · 81 ms · on page', 'hands · left seen 31 cm', 'room 59 frames/s'], 1, at(2))
    log.take(['/chat · aims    5/s · got  3.0/s', 'pinch · right · select · held · 300 ms · on page', 'hands · left lost at 50 cm', 'room 41 frames/s'], 1, at(4))
    log.take(['/chat · aims    5/s · got  3.0/s', 'pinch · right · select · held · 300 ms · on page', 'hands · left lost at 50 cm', 'room 41 frames/s'], 1, at(4.5))
    // The held pinch ends 0.3 s later: its end is kept at once; while it was held, nothing of it was.
    log.take(['/chat · aims   30/s · got  0.0/s', 'pinch · right · select · drag · 800 ms · on page', 'hands · left lost at 50 cm', 'room 41 frames/s'], 1, at(4.8))
    const lines = log.lines()
    expect(lines.filter(l => / pinch · /.test(l)).map(l => l.split(' · ')[3])).toEqual(['none yet', 'click', 'drag'].map(w => (w === 'none yet' ? undefined : w)))
    // The hands' line changed at 2 s and at 4 s: at most one in 10 s is kept after the first.
    expect(lines.filter(l => / hands · /.test(l))).toHaveLength(1)
    // The pace while held: the one read at 4 s (4.5 s is within its second), with the room's line.
    const held = lines.slice(lines.findIndex(l => /while a pinch was held/.test(l)) + 1)
    expect(held).toEqual(['+0:04 /chat · aims    5/s · got  3.0/s', '+0:04 room 41 frames/s'])
    // A load of the tab: the saved log comes back whole.
    const again = new NumbersLog()
    again.load(log.save())
    expect(again.lines()).toEqual(lines)
    again.load('not a log')
    expect(again.lines()).toEqual(lines)
  })

  it('starts again after a send, and past its cap cuts the oldest lines and keeps the held ones (N2, N3)', () => {
    const log = new NumbersLog()
    const at = (s: number) => 2_000_000 + s * 1000
    log.take(['pinch · right · select · click · 80 ms · on page'], 0, at(0))
    log.clear()
    log.take(['pinch · right · select · drag · 300 ms · on page'], 0, at(5))
    // After a send, only what came after it.
    expect(log.lines().filter(l => / pinch · /.test(l)).map(l => l.split(' · ')[3])).toEqual(['drag'])
    // Many lines and a held pinch with its round trips: the cut takes the oldest, the held lines all stay.
    for (let i = 0; i < 100; i += 1) log.take([`pinch · right · select · click · ${i} ms · on page`], 0, at(10 + i))
    log.take(['/chat · aims 5/s', 'pinch · right · select · held · 900 ms · on page'], 1, at(200), ['/chat · round trips ms · 30 40 900'])
    const lines = log.lines(2000)
    expect(lines.join('\n').length).toBeLessThanOrEqual(2000)
    expect(lines.some(l => /older lines cut/.test(l))).toBe(true)
    expect(lines.some(l => / 99 ms /.test(l))).toBe(true)
    expect(lines.some(l => / 0 ms /.test(l))).toBe(false)
    expect(lines.slice(-2)).toEqual(['+3:15 /chat · aims 5/s', '+3:15 /chat · round trips ms · 30 40 900'])
  })

  it('after a good send keeps the lines that came while it was on its way, for the next press, once (R2)', () => {
    const log = new NumbersLog()
    const at = (s: number) => 2_000_000 + s * 1000
    const kinds = () => log.lines().filter(l => / pinch · /.test(l)).map(l => l.split(' · ')[3])
    log.take(['pinch · right · select · click · 80 ms · on page'], 0, at(0))
    // The press at 1 s; a pinch at 2 s, while the send is on its way; the send's answer comes, and it clears.
    const press = log.mark(at(1))
    log.take(['pinch · right · select · drag · 300 ms · on page'], 0, at(2))
    log.clear(press)
    expect(kinds()).toEqual(['drag'])
    // The next press sends it, and after that nothing is left.
    log.clear(log.mark(at(3)))
    expect(log.lines()).toEqual([])
  })

  it('after a load of the tab, a good send leaves none of the loaded lines (M18)', () => {
    const at = (s: number) => 2_000_000 + s * 1000
    const before = new NumbersLog()
    for (let i = 0; i < 6; i += 1) before.take([`pinch · right · select · click · ${80 + i} ms · on page`], 0, at(i))
    // The tab loads again: a new log takes the saved one back, then one more line, then a press and its good answer.
    const log = new NumbersLog()
    log.load(before.save())
    log.take(['pinch · right · select · drag · 300 ms · on page'], 0, at(7))
    log.clear(log.mark(at(8)))
    expect(log.lines()).toEqual([])
  })

  it('keeps a line taken in the press\'s own millisecond after the press: it was not in the message (V7)', () => {
    const log = new NumbersLog()
    const at = (s: number) => 2_000_000 + s * 1000
    const kinds = () => log.lines().filter(l => / pinch · /.test(l)).map(l => l.split(' · ')[3])
    // Where the log stood at the press; a log with no mark (the file before V7) is cleared by the press's clock.
    const markAt = (t: number) => (typeof (log as { mark?: unknown }).mark === 'function' ? log.mark(t) : t) as Parameters<NumbersLog['clear']>[0]
    log.take(['pinch · right · select · click · 80 ms · on page'], 0, at(0))
    const press = markAt(at(1))
    // The message goes with the click; in the same millisecond, after it, the log takes a drag.
    log.take(['pinch · right · select · drag · 300 ms · on page'], 0, at(1))
    log.clear(press)
    expect(kinds()).toEqual(['drag'])
  })

  it('sends only to a Bot on his crew, whatever the link says (D59 1)', () => {
    const crew = [{ slug: 'android-xr' }, { slug: 'mira' }]
    expect(sendToOf(['android-xr'], crew)).toBe('android-xr')
    // Another letter case is the same Bot, given as its own slug; the same word twice is one word.
    expect(sendToOf(['Android-XR'], crew)).toBe('android-xr')
    expect(sendToOf(['mira', ' MIRA '], crew)).toBe('mira')
    // A word that is no Bot of his, two different Bots, nothing, or a crew not read yet: no Bot.
    expect(sendToOf(['someone-else'], crew)).toBeNull()
    expect(sendToOf(['mira', 'android-xr'], crew)).toBeNull()
    expect(sendToOf([], crew)).toBeNull()
    expect(sendToOf(['mira'], [])).toBeNull()
  })

  it('tells the plate where the send stands', () => {
    expect(sendLine({ phase: 'none' }, 'bot')).toBeNull()
    expect(sendLine({ phase: 'sending', at: 0 }, 'bot')).toContain('bot')
    expect(sendLine({ phase: 'sent', at: at.getTime(), chars: 10 }, 'bot')).toContain('bot')
    expect(sendLine({ phase: 'failed', at: at.getTime(), why: 'Failed to fetch' }, 'bot')).toContain('Failed to fetch')
  })
})
