import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { XrBot } from '../model'
import { LINE_WAIT_MS } from './home'
import { OFF_LINE_MS, WORDS_WAIT_MS, useHomeNames } from './useHomeNames'

type View = { bot: string; needId: string; where: 'bot' | 'held' | 'floor'; kind?: string; stamped?: boolean; writing?: boolean }
/** A Bot with `asks` approvals that a board can answer (`requestId`), or with asks that no board can (`bare`). */
const bot = (slug: string, asks: number, state = 'idle', bare = false) => ({ slug, label: slug, state, needs: Array.from({ length: asks }, (_, i) => ({ id: `${slug}${i + 1}`, ts: i, kind: bare ? 'attention' : 'approval', requestId: bare ? '' : `r${i}`, questions: [] })) }) as unknown as XrBot
const crew = [bot('atlas', 2), bot('pico', 0, 'failed'), bot('nova', 1, 'idle', true)]
const start = (views: View[], { inSession = true, done = [] as string[], drawn = ['atlas', 'pico', 'nova'] } = {}) => {
  const take = vi.fn(() => true)
  const tap = vi.fn()
  const openBot = vi.fn()
  const release = vi.fn(() => ({ kind: 'throw' as const, particles: true }))
  const answered = () => new Set(done)
  const anyDownAt = { current: -Infinity }
  const drawnBots = new Set(drawn)
  const hook = renderHook(({ v }) => useHomeNames({ crew, known: true, inSession, boards: { views: v as never, take, release, answered }, drawnBots, tap, openBot, anyDownAt }), { initialProps: { v: views } })
  return { ...hook, take, tap, openBot, release, anyDownAt }
}

afterEach(() => { vi.useRealTimers() })

describe('a pinch on a name of Home\'s line', () => {
  it('takes the board that stands at that Bot, by the board\'s own id, as a pinch on it at its desk does', () => {
    const { result, take, tap, openBot } = start([{ bot: 'atlas', needId: 'a1', where: 'bot' }])
    expect(result.current.does('atlas')).toBe('take')
    act(() => result.current.press('atlas'))
    expect(take.mock.calls).toEqual([['a1']])
    expect(tap).not.toHaveBeenCalled()
    expect(openBot).not.toHaveBeenCalled()
  })
  it('does nothing while that Bot\'s unstamped board is in his hand: it is never let go by a name', () => {
    const { result, take, release } = start([{ bot: 'atlas', needId: 'a1', where: 'held' }])
    expect(result.current.does('atlas')).toBe('none')
    act(() => result.current.press('atlas'))
    expect(take).not.toHaveBeenCalled()
    expect(release).not.toHaveBeenCalled()
  })
  it('never takes the sheet he speaks on', () => {
    const { result, take } = start([{ bot: 'atlas', needId: 's1', where: 'bot', kind: 'speak' }])
    act(() => result.current.press('atlas'))
    expect(take).not.toHaveBeenCalled()
  })
  it('opens the Bot\'s page when no board can come for its asks, and takes nothing (M23)', () => {
    const { result, take, openBot } = start([])
    expect(result.current.does('nova')).toBe('page')
    act(() => result.current.press('nova'))
    expect(openBot.mock.calls).toEqual([['nova']])
    expect(take).not.toHaveBeenCalled()
  })
  it('opens the page of a Bot that is not drawn: it holds up no board (M23)', () => {
    const { result, openBot } = start([], { drawn: ['pico', 'nova'] })
    expect(result.current.does('atlas')).toBe('page')
    act(() => result.current.press('atlas'))
    expect(openBot.mock.calls).toEqual([['atlas']])
  })
  it('never opens a page when a board can come: it waits for the board that is about to be made', () => {
    const { result, rerender, take, openBot } = start([])
    expect(result.current.does('atlas')).toBe('take')
    act(() => result.current.press('atlas'))
    rerender({ v: [{ bot: 'atlas', needId: 'atlas1', where: 'bot' }] })
    expect(take.mock.calls).toEqual([['atlas1']])
    expect(openBot).not.toHaveBeenCalled()
  })
  it('taps a failed Bot by the desk\'s tap, dims its name, and takes its last words as they come (M24)', () => {
    const { result, rerender, take, tap } = start([])
    act(() => result.current.press('pico'))
    expect(tap.mock.calls).toEqual([['pico']])
    expect(result.current.does('pico')).toBe('fetch')
    // Dimmed, it takes no second pinch.
    act(() => result.current.press('pico'))
    expect(tap).toHaveBeenCalledTimes(1)
    rerender({ v: [{ bot: 'pico', needId: 'w1', where: 'bot', kind: 'words' }] })
    expect(take.mock.calls).toEqual([['w1']])
    expect(result.current.does('pico')).not.toBe('fetch')
  })
  it('leaves words that come later than the wait at the Bot, and the name takes a pinch again', () => {
    vi.useFakeTimers()
    const { result, rerender, take } = start([])
    act(() => result.current.press('pico'))
    act(() => { vi.advanceTimersByTime(LINE_WAIT_MS + 1) })
    expect(result.current.does('pico')).toBe('fetch')
    act(() => { vi.advanceTimersByTime(WORDS_WAIT_MS) })
    expect(result.current.does('pico')).toBe('take')
    rerender({ v: [{ bot: 'pico', needId: 'w1', where: 'bot', kind: 'words' }] })
    expect(take).not.toHaveBeenCalled()
    act(() => result.current.press('pico'))
    expect(take.mock.calls).toEqual([['w1']])
  })
  it('takes no board that comes after he pressed something else', () => {
    const { result, rerender, take, anyDownAt } = start([])
    act(() => result.current.press('pico'))
    anyDownAt.current = Infinity
    rerender({ v: [{ bot: 'pico', needId: 'w1', where: 'bot', kind: 'words' }] })
    expect(take).not.toHaveBeenCalled()
  })
  it('does not count an ask that a board answered: at the stamp "atlas 2" is "atlas 1"', () => {
    const { result } = start([{ bot: 'atlas', needId: 'atlas1', where: 'held', stamped: true }], { done: ['atlas1'] })
    expect(result.current.names.names.find(n => n.slug === 'atlas')).toMatchObject({ asks: 1 })
    expect(result.current.does('atlas')).toBe('take')
  })
  it('files the stamped board in his hand as a throw does, and takes the Bot\'s next board as it comes', () => {
    const { result, rerender, take, release } = start([{ bot: 'atlas', needId: 'atlas1', where: 'held', stamped: true }], { done: ['atlas1'] })
    act(() => result.current.press('atlas'))
    expect(release.mock.calls).toEqual([['atlas1', { handM: 1, handMps: 10, onBot: false }]])
    expect(take).not.toHaveBeenCalled()
    rerender({ v: [{ bot: 'atlas', needId: 'atlas2', where: 'bot' }] })
    expect(take.mock.calls).toEqual([['atlas2']])
  })
  it('keeps a name in its place when its count goes down: "atlas 1" stands where "atlas 2" stood (D75)', () => {
    const done: string[] = []
    const { result, rerender } = start([], { done })
    expect(result.current.names.names.map(n => n.slug)).toEqual(['atlas', 'nova', 'pico'])
    done.push('atlas1')
    rerender({ v: [{ bot: 'atlas', needId: 'atlas1', where: 'held', stamped: true }] })
    // nova's ask is the older one: laid anew, nova would stand first.
    expect(result.current.names.names.map(n => `${n.slug} ${n.asks}`)).toEqual(['atlas 1', 'nova 1', 'pico 0'])
  })
  it('keeps the place of a name that leaves while his dot is on the line, and closes the gap when the dot is off', () => {
    vi.useFakeTimers()
    const done: string[] = []
    const { result, rerender } = start([], { done })
    act(() => result.current.over(true))
    done.push('atlas1', 'atlas2')
    rerender({ v: [{ bot: 'atlas', needId: 'atlas2', where: 'held', stamped: true }] })
    expect(result.current.names.names.map(n => `${n.slug}${n.gone ? ' gone' : ''}`)).toEqual(['atlas gone', 'nova', 'pico'])
    expect(result.current.does('atlas')).toBe('none')
    // From one name to the next the dot is off for a moment: no gap closes.
    act(() => { result.current.over(false); vi.advanceTimersByTime(OFF_LINE_MS - 50); result.current.over(true) })
    expect(result.current.names.names).toHaveLength(3)
    act(() => { result.current.over(false); vi.advanceTimersByTime(OFF_LINE_MS + 1) })
    expect(result.current.names.names.map(n => n.slug)).toEqual(['nova', 'pico'])
  })
  it('never files a stamped board he still writes on: the name does nothing (D76 2)', () => {
    const { result, release } = start([{ bot: 'atlas', needId: 'atlas1', where: 'held', stamped: true, writing: true }], { done: ['atlas1'] })
    expect(result.current.does('atlas')).toBe('none')
    act(() => result.current.press('atlas'))
    expect(release).not.toHaveBeenCalled()
  })
  it('files a stamped board whose note is finished as a throw files it, which sends the note (D76 2)', () => {
    const { result, release } = start([{ bot: 'atlas', needId: 'atlas1', where: 'held', stamped: true, writing: false }], { done: ['atlas1'] })
    act(() => result.current.press('atlas'))
    expect(release.mock.calls).toEqual([['atlas1', { handM: 1, handMps: 10, onBot: false }]])
  })
  it('taps a failed Bot only once its filed board has left the boards (W2)', () => {
    const { result, rerender, tap, release } = start([{ bot: 'pico', needId: 'p1', where: 'held', stamped: true }], { done: ['p1'] })
    act(() => result.current.press('pico'))
    expect(release).toHaveBeenCalledTimes(1)
    expect(tap).not.toHaveBeenCalled()
    rerender({ v: [] })
    expect(tap.mock.calls).toEqual([['pico']])
  })
  it('never shows more than `most` names while a gap is kept: the new askers that do not fit are in "+n" (B10)', () => {
    const boards = { views: [] as never, take: vi.fn(() => true), release: vi.fn(), answered: () => new Set<string>() }
    const four = ['atlas', 'juno', 'mira', 'otto'].map(s => bot(s, 1))
    const hook = renderHook(({ c }) => useHomeNames({ crew: c, known: true, inSession: true, boards: boards as never, drawnBots: new Set(['atlas', 'juno', 'mira', 'otto', 'pico', 'sage']), tap: vi.fn(), openBot: vi.fn(), anyDownAt: { current: -Infinity } }), { initialProps: { c: four } })
    act(() => hook.result.current.over(true))
    // Atlas stops asking while the dot is on the line; Pico and Sage begin to.
    hook.rerender({ c: [bot('atlas', 0), ...four.slice(1), bot('pico', 1), bot('sage', 1)] })
    const { names, more } = hook.result.current.names
    expect(names).toHaveLength(4)
    expect(names.map(n => `${n.slug}${n.gone ? ' gone' : ''}`)).toEqual(['atlas gone', 'juno', 'mira', 'otto'])
    expect(more).toBe(2)
  })
  it('has no names outside a session', () => {
    expect(start([], { inSession: false }).result.current.names).toEqual({ names: [], more: 0 })
  })
})
