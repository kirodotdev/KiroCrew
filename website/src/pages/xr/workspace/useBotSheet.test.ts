import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { XrBot } from '../model'
import { botSheetDoes, useBotSheet } from './useBotSheet'
import { WORDS_WAIT_MS } from './useHomeNames'

type View = { bot: string; needId: string; where: 'bot' | 'held' | 'floor'; kind?: string; writing?: boolean }
const bot = (slug: string, state: string, more: Partial<XrBot> = {}) => ({ slug, label: slug, state, rest: [], cuts: [], needs: [], ...more }) as unknown as XrBot
const failed = bot('pico', 'failed', { cuts: [{ slot: 'member-pico', id: 'c1', unread: true, stale: false }] as never })
const working = bot('otto', 'working')
const finished = bot('mira', 'idle', { rest: [{ slot: 'w-mira', unread: true, ts: 1, direct: true }] as never })
const quiet = bot('sage', 'idle')

describe('what a pinch on a Bot at its desk brings (D77)', () => {
  it('brings a sheet for a failed, a finished and a working Bot, and the page for one with nothing to say', () => {
    expect(botSheetDoes(failed, [], undefined)).toBe('raise')
    expect(botSheetDoes(finished, [], undefined)).toBe('raise')
    expect(botSheetDoes(working, [], undefined)).toBe('raise')
    expect(botSheetDoes(quiet, [], undefined)).toBe('page')
  })
  it('takes the board that stands at the Bot, and never the sheet he speaks on', () => {
    expect(botSheetDoes(failed, [{ bot: 'pico', needId: 'w1', where: 'bot', kind: 'words' }], undefined)).toBe('take')
    expect(botSheetDoes(quiet, [{ bot: 'sage', needId: 's1', where: 'bot', kind: 'speak' }], undefined)).toBe('page')
  })
  it('brings nothing new while its sheet is in his hand', () => {
    expect(botSheetDoes(failed, [{ bot: 'pico', needId: 'w1', where: 'held', kind: 'words' }], undefined)).toBe('held')
  })
  it('gives no sheet while the Bot walks or he carries it (D10)', () => {
    expect(botSheetDoes(failed, [], { walking: true } as never)).toBe('none')
    expect(botSheetDoes(failed, [], { carried: true } as never)).toBe('none')
  })
  it('takes nothing while he writes on two boards in his hand (D79 2)', () => {
    const two: View[] = [{ bot: 'atlas', needId: 'a1', where: 'held', writing: true }, { bot: 'juno', needId: 'j1', where: 'held', writing: true }]
    expect(botSheetDoes(failed, two, undefined)).toBe('none')
  })
})

afterEach(() => { vi.useRealTimers() })

describe('useBotSheet', () => {
  const start = (views: View[], inSession = true) => {
    const take = vi.fn(() => true)
    const tap = vi.fn()
    const openBot = vi.fn()
    const say = vi.fn()
    const anyDownAt = { current: -Infinity }
    const hook = renderHook(({ v }) => useBotSheet({ crew: [failed, quiet], views: v, inSession, body: () => undefined, take, tap, openBot, say, anyDownAt }), { initialProps: { v: views } })
    return { ...hook, take, tap, openBot, say, anyDownAt }
  }
  it('raises a failed Bot\'s words by the desk\'s tap and takes them as they stand at it', () => {
    const { result, rerender, take, tap } = start([])
    act(() => result.current.press('pico'))
    expect(tap.mock.calls).toEqual([['pico']])
    rerender({ v: [{ bot: 'pico', needId: 'w:member-pico', where: 'bot', kind: 'words' }] })
    expect(take.mock.calls).toEqual([['w:member-pico']])
  })
  it('never takes a new ask that comes while its words are on their way (D83 6)', () => {
    const { result, rerender, take } = start([])
    act(() => result.current.press('pico'))
    rerender({ v: [{ bot: 'pico', needId: 'a:new-ask', where: 'bot', kind: 'ask' }] })
    expect(take).not.toHaveBeenCalled()
  })
  it('leaves words that come after the wait at the Bot', () => {
    vi.useFakeTimers()
    const { result, rerender, take } = start([])
    act(() => result.current.press('pico'))
    act(() => { vi.advanceTimersByTime(WORDS_WAIT_MS + 1) })
    rerender({ v: [{ bot: 'pico', needId: 'w:member-pico', where: 'bot', kind: 'words' }] })
    expect(take).not.toHaveBeenCalled()
  })
  it('opens the page of a Bot with nothing to say, and says so when its sheet is in his hand', () => {
    const { result, openBot, say, take } = start([{ bot: 'pico', needId: 'w1', where: 'held', kind: 'words' }])
    act(() => result.current.press('sage'))
    expect(openBot.mock.calls).toEqual([['sage']])
    act(() => result.current.press('pico'))
    expect(say.mock.calls.map(c => c[0])).toEqual(['pico'])
    expect(take).not.toHaveBeenCalled()
  })
  it('brings a read sheet again after he let it go: the Bot has nothing new, and its page does not open (D82 2)', () => {
    const { result, rerender, tap, openBot } = start([{ bot: 'sage', needId: 'w:member-sage', where: 'held', kind: 'words' }])
    rerender({ v: [] })
    act(() => result.current.press('sage'))
    expect(tap.mock.calls).toEqual([['sage']])
    expect(openBot).not.toHaveBeenCalled()
  })
  it('is the desk\'s tap outside a session', () => {
    const { result, tap, openBot } = start([], false)
    act(() => result.current.press('sage'))
    expect(tap.mock.calls).toEqual([['sage']])
    expect(openBot).not.toHaveBeenCalled()
  })
})
