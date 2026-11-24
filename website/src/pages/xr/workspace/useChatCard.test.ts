import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { Vector3 } from 'three'
import { HOLD } from './speak'
import { useBoards } from './useBoards'
import { newCard } from './chatCard'
import { pinchRoute } from './pinchRoute'
import { CARD_DOT_AWAY_MS, CARD_DOT_BACK_MS, CARD_SWITCH_GLIDE_MS, cardTakesDot, KEYS_SWITCH_QUIET_MS, KEYS_SWITCH_SETTLE_MS, keysSwitchWaitMs, useChatCard } from './useChatCard'
import { useComment } from './useComment'

// A send in these tests fails: the card that was thrown comes back as a failed card.
vi.mock('../writes', async original => ({ ...(await original<typeof import('../writes')>()), cardSend: () => Promise.reject(new Error('no gateway here')), cardChatLeft: () => false }))

// A room with no Bots and no desk; this browser has no speech recognizer, so the card types by its keyboard.
const host = { head: () => new Vector3(0, 1.6, 0), lookYawDeg: () => 0, desk: () => null, bots: () => [], bodies: () => [], openPage: () => {}, say: () => {} }

describe('the new chat card\'s keyboard', () => {
  it('is put away while a board is in his hand, and comes back with the card\'s words', () => {
    const { result } = renderHook(() => useChatCard(host))
    act(() => { result.current.open() })
    expect(result.current.keys).not.toBeNull()
    act(() => { result.current.typed('h') })
    act(() => { result.current.keysAway(true) })
    expect(result.current.keys).toBeNull()
    // A real keyboard's typing key, while the card's keyboard is away, is taken and types nothing: no page behind
    // gets it. An arrow still goes on, to turn the head.
    let taken = false
    act(() => { taken = result.current.typed('i') })
    expect(taken).toBe(true)
    expect(result.current.card?.text).toBe('h')
    expect(result.current.typed('ArrowLeft')).toBe(false)
    act(() => { result.current.keysAway(false) })
    expect(result.current.keys).not.toBeNull()
    expect(result.current.card?.text).toBe('h')
  })
})

describe('the new chat card\'s speech', () => {
  const w = window as unknown as { SpeechRecognition?: unknown }
  afterEach(() => { delete w.SpeechRecognition })

  it('does not listen while a hold\'s sheet listens, and listens again when the hold ends (review, 09:49)', () => {
    const log: string[] = []
    w.SpeechRecognition = class {
      lang = ''
      onstart: (() => void) | null = null
      onresult: unknown = null
      onerror: unknown = null
      onend: (() => void) | null = null
      start() { log.push('start') }
      stop() { log.push('stop') }
      abort() { log.push('stop') }
    }
    const { result } = renderHook(() => useChatCard(host))
    act(() => { result.current.open() })
    expect(log).toEqual(['start'])
    act(() => { result.current.micAway(true) })
    expect(log).toEqual(['start', 'stop'])
    act(() => { result.current.micAway(false) })
    expect(log).toEqual(['start', 'stop', 'start'])
  })

  it('stands aside for a hold in order: card stops, hold starts, hold stops, card starts (review, 10:10)', () => {
    vi.useFakeTimers()
    onTestFinished(() => { vi.useRealTimers() })
    const log: string[] = []
    let made = 0
    w.SpeechRecognition = class {
      id = (made += 1)
      lang = ''
      onstart: (() => void) | null = null
      onresult: unknown = null
      onerror: unknown = null
      onend: (() => void) | null = null
      start() { log.push(`start ${this.id}`) }
      stop() { log.push(`stop ${this.id}`) }
      abort() { log.push(`stop ${this.id}`) }
    }
    // The workspace's own wiring of the three: the card, the boards (a hold's sheet), the note's line.
    const { result } = renderHook(() => {
      const card = useChatCard(host)
      const boards = useBoards([], { say: () => {}, openBot: () => {}, micAway: card.micAway })
      const writingOn = boards.views.find(v => v.writing && v.kind === 'speak') ?? null
      useComment(writingOn ? { id: writingOn.needId, comment: writingOn.comment } : null, boards.note, boards.foot, { head: () => new Vector3(), lookYawDeg: () => 0, bots: () => [], say: () => {} })
      return { card, boards }
    })
    act(() => { result.current.card.open() })
    act(() => { result.current.boards.holdStart('juno') })
    expect(log).toEqual(['start 1'])
    act(() => { vi.advanceTimersByTime(HOLD.listenAfterMs) })
    expect(log).toEqual(['start 1', 'stop 1', 'start 2'])
    act(() => { result.current.boards.holdEnd('juno', 'let') })
    expect(log).toEqual(['start 1', 'stop 1', 'start 2', 'stop 2', 'start 3'])
  })
})

describe('the card and its keys at a room switch (D144)', () => {
  /** A Bot's feet on the floor, so many degrees to the right of his head, 4 m away: the card and its keys lie over it in his view. */
  const feetAt = (yawDeg: number): [number, number, number] => { const r = (yawDeg * Math.PI) / 180; return [Math.sin(r) * 4, 0, -Math.cos(r) * 4] }
  /** How far to the right of straight ahead a point stands, seen from his head, in degrees. */
  const yawOf = (at: readonly number[]) => (Math.atan2(at[0], -at[2]) * 180) / Math.PI
  const AFTER = KEYS_SWITCH_SETTLE_MS + CARD_SWITCH_GLIDE_MS + 50

  /** A card that is out in room `r1`, and a Bot that asks, whose place the test sets. */
  const room = (desk: unknown = null) => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance', 'Date'] })
    onTestFinished(() => { vi.useRealTimers() })
    // `frames`: when the dot's pointer had a frame of its own; null: a host that cannot say (M171).
    const world = { bots: [] as { slug: string; state: string }[], bodies: [] as { slug: string; feet: [number, number, number]; top: [number, number, number] }[], quietUntil: 0, frames: null as number[] | null, seen: null as ((t: number) => boolean) | null, quiet: null as (() => number) | null }
    const hook = renderHook(({ room }) => useChatCard({ ...host, desk: () => desk as never, bots: () => world.bots as never, bodies: () => world.bodies as never, room, quietUntil: () => (world.quiet ? world.quiet() : world.quietUntil), dotSeenSince: t => (world.seen ? world.seen(t) : world.frames === null || world.frames.filter(f => f > t).length >= 2) }), { initialProps: { room: 'r1' } })
    act(() => { hook.result.current.open() })
    const first = { keys: hook.result.current.keys!, home: hook.result.current.home! }
    /** A Bot that asks comes to stand under the card and its keys, where they stand now. */
    const botAt = (yawDeg: number) => {
      world.bots = [{ slug: 'atlas', state: 'needs' }]
      world.bodies = [{ slug: 'atlas', feet: feetAt(yawDeg), top: [0, 1.2, -1.6] }]
    }
    const botUnder = () => botAt(yawOf(hook.result.current.keys!.at))
    const go = (to: string) => hook.rerender({ room: to })
    const pass = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })
    const still = () => { expect(hook.result.current.keys).toBe(first.keys); expect(hook.result.current.home).toBe(first.home) }
    return { hook, first, world, botAt, botUnder, go, pass, still }
  }

  it('inside one room neither moves, whatever comes to stand under them (D110)', () => {
    const r = room()
    r.botUnder()
    r.go('r1')
    r.pass(5000)
    r.still()
  })

  it('in a new room with no Bot that asks under them, neither moves', () => {
    const r = room()
    // A Bot that asks stands far to the other side: not under the card, not under its keys.
    r.botAt(-3 * yawOf(r.first.keys.at))
    r.go('r2')
    r.pass(AFTER + 2000)
    r.still()
  })

  it('a card that glided to its other side stays there in a room where no Bot asks: it does not go back', () => {
    const r = room()
    r.botUnder()
    r.go('r2')
    r.pass(AFTER)
    const [keys, home] = [r.hook.result.current.keys, r.hook.result.current.home]
    expect(keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
    r.botAt(170)
    r.go('r3')
    r.pass(AFTER + 2000)
    expect(r.hook.result.current.keys).toBe(keys)
    expect(r.hook.result.current.home).toBe(home)
  })

  it('with a Bot that asks under them in the new room, both glide once, in 300 ms, to the same free side', () => {
    const r = room()
    r.botUnder()
    r.go('r2')
    r.pass(KEYS_SWITCH_SETTLE_MS - 20)
    r.still()
    // Half way: both are between the two sides, together.
    r.pass(20 + CARD_SWITCH_GLIDE_MS / 2)
    const mid = r.hook.result.current
    expect(Math.abs(mid.keys!.yawDeg)).toBeLessThan(Math.abs(r.first.keys.yawDeg))
    expect(yawOf(mid.home!)).toBeCloseTo(mid.keys!.yawDeg, 3)
    r.pass(CARD_SWITCH_GLIDE_MS / 2 + 50)
    const end = r.hook.result.current
    expect(end.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
    // The card stands over its keys, on the same side, as when it came out.
    expect(yawOf(end.home!)).toBeCloseTo(end.keys!.yawDeg, 3)
    expect(yawOf(r.first.home)).toBeCloseTo(r.first.keys.yawDeg, 3)
    expect(end.home![1]).toBeCloseTo(r.first.home[1], 6)
    // Once: the Bot walks under the new place, in this same room, and nothing moves again.
    r.botUnder()
    r.go('r2')
    r.pass(5000)
    expect(r.hook.result.current.keys).toBe(end.keys)
    expect(r.hook.result.current.home).toBe(end.home)
  })

  it('a card on the desk never moves', () => {
    const r = room({ at: [0, 0.72, -0.5], yawDeg: 0, from: 'default', inSession: true, confirmed: true, widthM: 1.2, depthM: 0.6 })
    expect(r.first.keys.flat).toBe(true)
    r.botUnder()
    r.go('r2')
    r.pass(AFTER + 2000)
    r.still()
  })

  it('while he types nothing moves; it is tried again once his last key is a second old (M130)', () => {
    const r = room()
    r.botUnder()
    r.go('r2')
    // A key 100 ms before the place is judged, and one more 600 ms later.
    r.pass(KEYS_SWITCH_SETTLE_MS - 100)
    act(() => { r.hook.result.current.typed('h') })
    r.pass(600)
    act(() => { r.hook.result.current.typed('i') })
    // A second after the first key the place is tried again: the second key is 400 ms old, and nothing moves.
    r.pass(KEYS_SWITCH_QUIET_MS - 600 + 50)
    expect(r.hook.result.current.keys).toBe(r.first.keys)
    expect(r.hook.result.current.home).toBe(r.first.home)
    // A second after the second key they glide.
    r.pass(600 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
    expect(r.hook.result.current.card?.text).toBe('hi')
  })

  it('two switches in a row move them at most once, after the last (M130)', () => {
    const r = room()
    r.botUnder()
    r.go('r2')
    r.pass(KEYS_SWITCH_SETTLE_MS - 100)
    r.go('r3')
    // The first switch's time has come and gone: nothing is judged for it.
    r.pass(KEYS_SWITCH_SETTLE_MS - 50)
    r.still()
    r.pass(50 + CARD_SWITCH_GLIDE_MS + 50)
    const end = r.hook.result.current.keys
    expect(end!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
    r.pass(5000)
    expect(r.hook.result.current.keys).toBe(end)
  })

  it('there and back within 400 ms: nothing moves', () => {
    const r = room()
    r.botUnder()
    r.go('r2')
    r.pass(KEYS_SWITCH_SETTLE_MS - 100)
    r.go('r1')
    r.pass(AFTER + 5000)
    r.still()
  })

  it('a failed card is not moved', async () => {
    const r = room()
    act(() => { r.hook.result.current.typed('o') })
    act(() => { r.hook.result.current.act({ type: 'grab' }) })
    act(() => { r.hook.result.current.act({ type: 'release', target: { to: 'open' } }) })
    act(() => { r.hook.result.current.act({ type: 'landed' }) })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(r.hook.result.current.card).toMatchObject({ phase: 'out', failed: true })
    const [keys, home] = [r.hook.result.current.keys, r.hook.result.current.home]
    r.pass(KEYS_SWITCH_QUIET_MS + 100)
    // Its keys do not show; the Bot stands under where they and the card stood.
    r.botAt(yawOf(r.first.keys.at))
    r.go('r2')
    r.pass(AFTER + 2000)
    expect(r.hook.result.current.keys).toBe(keys)
    expect(r.hook.result.current.home).toBe(home)
  })

  // ── his dot on the card or its keys (M145) ──

  it.each(['card', 'keys'] as const)('does not start the glide while his dot is on the %s; it starts 400 ms after the dot left', part => {
    const r = room()
    r.botUnder()
    act(() => { r.hook.result.current.dot(part, true) })
    r.go('r2')
    r.pass(AFTER + 3000)
    r.still()
    act(() => { r.hook.result.current.dot(part, false) })
    r.pass(CARD_DOT_AWAY_MS - 10)
    r.still()
    r.pass(10 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
    expect(yawOf(r.hook.result.current.home!)).toBeCloseTo(r.hook.result.current.keys!.yawDeg, 3)
  })

  it('his dot goes from the card onto the keys: the wait starts anew when it leaves the last of them', () => {
    const r = room()
    r.botUnder()
    act(() => { r.hook.result.current.dot('card', true) })
    r.go('r2')
    r.pass(AFTER)
    act(() => { r.hook.result.current.dot('card', false) })
    r.pass(CARD_DOT_AWAY_MS - 100)
    act(() => { r.hook.result.current.dot('keys', true) })
    r.pass(3000)
    r.still()
    act(() => { r.hook.result.current.dot('keys', false) })
    r.pass(CARD_DOT_AWAY_MS - 10)
    r.still()
    r.pass(10 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('two dots, one on the card and one on the keys: one leaves, and nothing moves while the other is on', () => {
    const r = room()
    r.botUnder()
    act(() => { r.hook.result.current.dot('card', true); r.hook.result.current.dot('keys', true) })
    r.go('r2')
    r.pass(AFTER)
    act(() => { r.hook.result.current.dot('card', false) })
    r.pass(CARD_DOT_AWAY_MS + 3000)
    r.still()
    act(() => { r.hook.result.current.dot('keys', false) })
    r.pass(CARD_DOT_AWAY_MS - 10)
    r.still()
    r.pass(10 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('a card that is thrown takes its dot with it: it takes no pointer while it flies', () => {
    expect(cardTakesDot(null)).toBe(false)
    for (const phase of ['out', 'typing', 'held'] as const) expect(cardTakesDot({ ...newCard('c', false), phase })).toBe(true)
    for (const phase of ['flying', 'sending', 'sent'] as const) expect(cardTakesDot({ ...newCard('c', false), phase })).toBe(false)
  })

  // The room as it is: the new room's pages come, the quiet time runs 500 ms, and the dot's pointer is off in it.
  it('the quiet time of a switch turns his dot\'s pointer off: the leave it sends is not his, and nothing moves while his dot is on the card', () => {
    const r = room()
    r.botUnder()
    act(() => { r.hook.result.current.dot('card', true) })
    r.go('r2')
    r.pass(340)
    r.world.quietUntil = performance.now() + 500
    act(() => { r.hook.result.current.dot('card', false) })
    r.pass(505)
    // The quiet time is up: the pointer is on again, and the card is told the dot is on it.
    act(() => { r.hook.result.current.dot('card', true) })
    r.pass(5000)
    r.still()
    act(() => { r.hook.result.current.dot('card', false) })
    r.pass(CARD_DOT_AWAY_MS - 10)
    r.still()
    r.pass(10 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('he looks away in the quiet time: they glide 100 ms after its end, and not in it', () => {
    const r = room()
    r.botUnder()
    act(() => { r.hook.result.current.dot('keys', true) })
    r.go('r2')
    r.pass(340)
    r.world.quietUntil = performance.now() + 500
    act(() => { r.hook.result.current.dot('keys', false) })
    // Not in the quiet time, and not before the pointer could be on again.
    r.pass(500 + CARD_DOT_BACK_MS - 10)
    r.still()
    r.pass(10 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('a judging that falls just after the quiet time, before the pointer is on again, waits for it', () => {
    const r = room()
    r.botUnder()
    // The quiet time began before the switch and ends 350 ms after it: the judging comes at 400 ms.
    r.world.quietUntil = performance.now() + 350
    r.go('r2')
    r.pass(KEYS_SWITCH_SETTLE_MS + 10)
    r.still()
    act(() => { r.hook.result.current.dot('card', true) })
    r.pass(5000)
    r.still()
  })

  it('he looks away and no quiet time runs: they glide 400 ms later, the quiet time long past', () => {
    const r = room()
    r.botUnder()
    r.world.quietUntil = performance.now() - 5000
    act(() => { r.hook.result.current.dot('card', true) })
    r.go('r2')
    r.pass(AFTER)
    act(() => { r.hook.result.current.dot('card', false) })
    r.pass(CARD_DOT_AWAY_MS - 10)
    r.still()
    r.pass(10 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('a quiet time that is made longer (a pinch in it) holds the judging to its new end', () => {
    const r = room()
    r.botUnder()
    act(() => { r.hook.result.current.dot('card', true) })
    r.go('r2')
    r.pass(340)
    r.world.quietUntil = performance.now() + 500
    act(() => { r.hook.result.current.dot('card', false) })
    r.pass(300)
    r.world.quietUntil = performance.now() + 500
    r.pass(505)
    act(() => { r.hook.result.current.dot('card', true) })
    r.pass(5000)
    r.still()
  })

  // ── pointers are counted for each part (M170): his dot is pointer 1, a raised hand's ray pointer 2 ──

  it('his dot is on the keys, a hand\'s ray comes onto them, the dot leaves: no glide starts under the ray', () => {
    const r = room()
    r.botUnder()
    act(() => { r.hook.result.current.dot('keys', true, 1) })
    r.go('r2')
    r.pass(AFTER)
    act(() => { r.hook.result.current.dot('keys', true, 2) })
    act(() => { r.hook.result.current.dot('keys', false, 1) })
    r.pass(CARD_DOT_AWAY_MS + 5000)
    r.still()
    // The hand's ray leaves too: now they glide, 400 ms later.
    act(() => { r.hook.result.current.dot('keys', false, 2) })
    r.pass(CARD_DOT_AWAY_MS - 10)
    r.still()
    r.pass(10 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('the hand\'s ray leaves first: his dot still holds them; both gone, they glide 400 ms after the last', () => {
    const r = room()
    r.botUnder()
    act(() => { r.hook.result.current.dot('card', true, 1); r.hook.result.current.dot('card', true, 2) })
    r.go('r2')
    r.pass(AFTER)
    act(() => { r.hook.result.current.dot('card', false, 2) })
    r.pass(CARD_DOT_AWAY_MS + 3000)
    r.still()
    act(() => { r.hook.result.current.dot('card', false, 1) })
    r.pass(CARD_DOT_AWAY_MS - 10)
    r.still()
    r.pass(10 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('a leave of a pointer that was never on a part frees nothing, and an enter told twice is one', () => {
    const r = room()
    r.botUnder()
    act(() => { r.hook.result.current.dot('keys', true, 1); r.hook.result.current.dot('keys', true, 1) })
    r.go('r2')
    r.pass(AFTER)
    act(() => { r.hook.result.current.dot('keys', false, 2); r.hook.result.current.dot('card', false, 1) })
    r.pass(CARD_DOT_AWAY_MS + 3000)
    r.still()
    act(() => { r.hook.result.current.dot('keys', false, 1) })
    r.pass(CARD_DOT_AWAY_MS + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('pointers that are lost with no leave of their own are freed when their part goes: the keys put away, the card closed', () => {
    const r = room()
    act(() => { r.hook.result.current.dot('keys', true, 1); r.hook.result.current.dot('keys', true, 2); r.hook.result.current.dot('card', true, 2) })
    // The keys are put away under both pointers; the card is closed under one, and a new one comes out.
    act(() => { r.hook.result.current.keysAway(true) })
    act(() => { r.hook.result.current.keysAway(false) })
    act(() => { r.hook.result.current.close() })
    act(() => { r.hook.result.current.open() })
    const [keys, home] = [r.hook.result.current.keys!, r.hook.result.current.home!]
    r.botUnder()
    r.go('r2')
    r.pass(AFTER)
    expect(r.hook.result.current.keys).not.toBe(keys)
    expect(r.hook.result.current.home).not.toBe(home)
  })

  // ── the judging waits for the dot's pointer to be on again (M171) ──

  it('a frame of 150 ms at the quiet end: the judging waits for the dot\'s own frames, and nothing moves under his dot', () => {
    const r = room()
    r.botUnder()
    r.world.frames = []
    act(() => { r.hook.result.current.dot('card', true, 1) })
    r.go('r2')
    r.pass(340)
    r.world.quietUntil = performance.now() + 500
    act(() => { r.hook.result.current.dot('card', false, 1) })
    // The quiet time ends, and the frame that would turn the pointer on again takes 150 ms.
    r.pass(500 + 149)
    r.still()
    r.world.frames.push(performance.now())
    act(() => { r.hook.result.current.dot('card', true, 1) })
    r.pass(11)
    r.world.frames.push(performance.now())
    r.pass(5000)
    r.still()
  })

  it('he looked away in the quiet time and the dot\'s frames run on: they glide once the pointer could be on again', () => {
    const r = room()
    r.botUnder()
    r.world.frames = []
    act(() => { r.hook.result.current.dot('keys', true, 1) })
    r.go('r2')
    r.pass(340)
    r.world.quietUntil = performance.now() + 500
    act(() => { r.hook.result.current.dot('keys', false, 1) })
    r.pass(500 + 11)
    r.world.frames.push(performance.now())
    r.pass(11)
    r.world.frames.push(performance.now())
    r.pass(CARD_DOT_BACK_MS - 22 - 10)
    r.still()
    r.pass(10 + 20 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('no frame of the dot\'s runs after the quiet time (the page is hidden): nothing is judged until two have', () => {
    const r = room()
    r.botUnder()
    r.world.frames = []
    r.world.quietUntil = performance.now() + 300
    r.go('r2')
    r.pass(300 + 4000)
    r.still()
    r.world.frames.push(performance.now())
    r.pass(1000)
    r.still()
    r.world.frames.push(performance.now())
    r.pass(20 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  /**
   * The host's OWN object, as `Workspace.tsx` writes it today (M217): the text of the object that it hands to
   * `useChatCard` is read from the file and made an object, over stand-ins for the names it uses and the room's real
   * `pinchRoute`. So a key that is taken away, written twice or changed there is a change to what these units run.
   */
  const hostObject = (inXr: () => boolean, quietFlat = 0): { quietUntil: () => number; dotSeenSince: (t: number) => boolean } => {
    const text = readFileSync(resolve(process.cwd(), 'src/pages/xr/workspace/Workspace.tsx'), 'utf-8')
    const from = text.indexOf('const chatCard = useChatCard({')
    expect(from).toBeGreaterThan(0)
    expect(text.indexOf('const chatCard = useChatCard({', from + 1)).toBe(-1)
    const open = text.indexOf('{', from)
    const close = text.indexOf('\n  })\n', open)
    expect(close).toBeGreaterThan(open)
    const body = text.slice(open, close + 4)
    const names = ['bridge', 'deskRef', 'inXr', 'pinchRoute', 'quiet', 'crewNow', 'crewRead', 'openPage', 'say', 'rooms']
    const stand = [{ current: { head: new Vector3(0, 1.6, 0), lookYawDeg: 0 } }, { current: null }, inXr, pinchRoute, { current: quietFlat }, { current: [] }, { current: null }, () => {}, () => {}, { rooms: { now: 'r1' } }]
    // The host's object itself is what is run, not a copy of it.
    return new Function(...names, `return (${body})`)(...stand) as { quietUntil: () => number; dotSeenSince: (t: number) => boolean }
  }
  const hostLine = (inXr: () => boolean): ((t: number) => boolean) => hostObject(inXr).dotSeenSince
  /** The room's rules with no frame of the dot's and no quiet time, and so again after the test. */
  const freshRoute = () => {
    const clear = () => { pinchRoute.dotFrame(-Infinity); pinchRoute.dotFrame(-Infinity); pinchRoute.quietUntil = 0 }
    clear()
    onTestFinished(clear)
  }

  it('the host\'s one line (M194): in the flat preview it says yes; in the room it says what the dot\'s own frames say', () => {
    freshRoute()
    // The flat preview has no such frames: yes, whatever the room's rules hold.
    expect(hostLine(() => false)(2000)).toBe(true)
    // In the room: no frame yet, a frame inside the quiet time, one after its end: no. Two after its end: yes.
    const inRoom = hostLine(() => true)
    expect(inRoom(2000)).toBe(false)
    pinchRoute.dotFrame(1900)
    expect(inRoom(2000)).toBe(false)
    pinchRoute.dotFrame(2150)
    expect(inRoom(2000)).toBe(false)
    pinchRoute.dotFrame(2161)
    expect(inRoom(2000)).toBe(true)
    // And it asks about the time it is given, not about any time.
    expect(inRoom(2155)).toBe(false)
  })

  it('the host hands both lines on (M217): the quiet end is the room\'s in a session and the flat preview\'s own outside one', () => {
    freshRoute()
    const keys = Object.keys(hostObject(() => true))
    expect(keys.filter(k => k === 'dotSeenSince' || k === 'quietUntil').sort()).toEqual(['dotSeenSince', 'quietUntil'])
    pinchRoute.quietUntil = 4321
    expect(hostObject(() => true, 77).quietUntil()).toBe(4321)
    expect(hostObject(() => false, 77).quietUntil()).toBe(77)
    expect(typeof hostObject(() => true).dotSeenSince).toBe('function')
    expect(hostObject(() => true).dotSeenSince(4321)).toBe(false)
  })

  it('a frame of the dot\'s inside the quiet time does not count (M195): the judging waits for two frames after the quiet end, through the host\'s line', () => {
    freshRoute()
    const r = room()
    r.botUnder()
    // The hook is given the host's own two lines, over the room's real rules.
    const own = hostObject(() => true)
    r.world.seen = t => own.dotSeenSince(t)
    r.world.quiet = () => own.quietUntil()
    const frame = () => pinchRoute.dotFrame(performance.now())
    act(() => { r.hook.result.current.dot('keys', true, 1) })
    r.go('r2')
    r.pass(340)
    pinchRoute.quietUntil = performance.now() + 500
    // The quiet time turns his dot's pointer off, and he looks away in it. A frame of the dot's runs INSIDE it.
    act(() => { r.hook.result.current.dot('keys', false, 1) })
    r.pass(250)
    frame()
    // The quiet end, then a long frame: ONE frame of the dot's after the end. With the frame inside, that is two
    // frames since the quiet START, and still one since its end: nothing is judged, however long it lasts.
    r.pass(250 + 149)
    frame()
    r.pass(3000)
    r.still()
    // The second frame after the end: now it is judged. He has looked away for long, so they glide.
    frame()
    r.pass(20 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('a dot that left less than 400 ms before the judging holds the glide for the rest of that time', () => {
    const r = room()
    r.botUnder()
    act(() => { r.hook.result.current.dot('keys', true) })
    r.go('r2')
    r.pass(KEYS_SWITCH_SETTLE_MS - 100)
    act(() => { r.hook.result.current.dot('keys', false) })
    // The judging comes 100 ms after the dot left: 300 ms more pass before the glide.
    r.pass(100 + CARD_DOT_AWAY_MS - 100 - 10)
    r.still()
    r.pass(10 + CARD_SWITCH_GLIDE_MS + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('a glide under way is not stopped half-way when his dot comes onto them', () => {
    const r = room()
    r.botUnder()
    r.go('r2')
    r.pass(KEYS_SWITCH_SETTLE_MS + CARD_SWITCH_GLIDE_MS / 2)
    const mid = r.hook.result.current.keys!.yawDeg
    expect(Math.abs(mid)).toBeLessThan(Math.abs(r.first.keys.yawDeg))
    act(() => { r.hook.result.current.dot('keys', true) })
    r.pass(CARD_SWITCH_GLIDE_MS / 2 + 50)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
  })

  it('a switch while his dot is on them, then a second switch: the first judging is dropped with its wait', () => {
    const r = room()
    r.botUnder()
    act(() => { r.hook.result.current.dot('card', true) })
    r.go('r2')
    r.pass(AFTER)
    // He goes back to the room where the card was last judged; then the dot leaves. Nothing waits for it any more.
    r.go('r1')
    act(() => { r.hook.result.current.dot('card', false) })
    r.pass(AFTER + CARD_DOT_AWAY_MS + 2000)
    r.still()
  })

  // ── the lines that had no unit (M144) ──

  /** A Bot that asks, so high that it lies over the card and not over its keys, so many degrees to the right. */
  const overCardOnly = (yawDeg: number): [number, number, number] => { const [x, , z] = feetAt(yawDeg); return [x, 1.75, z] }

  it('a Bot that asks over the card alone, its keys clear, moves both', () => {
    const r = room()
    const world = r.world
    world.bots = [{ slug: 'atlas', state: 'needs' }]
    world.bodies = [{ slug: 'atlas', feet: overCardOnly(yawOf(r.first.home)), top: [0, 2.2, -1.6] }]
    r.go('r2')
    r.pass(AFTER)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
    expect(yawOf(r.hook.result.current.home!)).toBeCloseTo(r.hook.result.current.keys!.yawDeg, 3)
  })

  it('the new place is clear for the card too: a side whose card would lie over a Bot that asks is passed by', () => {
    const r = room()
    const own = yawOf(r.first.keys.at)
    r.world.bots = [{ slug: 'atlas', state: 'needs' }, { slug: 'juno', state: 'needs' }]
    // One Bot under them where they stand; another over the card alone at the other side.
    r.world.bodies = [{ slug: 'atlas', feet: feetAt(own), top: [0, 1.2, -1.6] }, { slug: 'juno', feet: overCardOnly(-own), top: [0, 2.2, -1.6] }]
    r.go('r2')
    r.pass(AFTER)
    // Not the other side (the card would lie over Juno): twice as far on their own side.
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(2 * r.first.keys.yawDeg, 6)
    expect(yawOf(r.hook.result.current.home!)).toBeCloseTo(r.hook.result.current.keys!.yawDeg, 3)
  })

  it('the next room is judged from the place they have now, not from where the card came out', () => {
    const r = room()
    r.botUnder()
    r.go('r2')
    r.pass(AFTER)
    const second = r.hook.result.current.keys!
    expect(second.yawDeg).toBeCloseTo(-r.first.keys.yawDeg, 6)
    // In the next room a Bot asks under the place they have now, and none where the card came out: they move again.
    r.botUnder()
    r.go('r3')
    r.pass(AFTER)
    expect(r.hook.result.current.keys!.yawDeg).toBeCloseTo(r.first.keys.yawDeg, 6)
  })

  it('a glide that is under way stops when a new card comes out: the new card stands where it came out', () => {
    const r = room()
    r.botUnder()
    r.go('r2')
    r.pass(KEYS_SWITCH_SETTLE_MS + CARD_SWITCH_GLIDE_MS / 2)
    act(() => { r.hook.result.current.close() })
    r.world.bots = []
    act(() => { r.hook.result.current.open() })
    const [keys, home] = [r.hook.result.current.keys, r.hook.result.current.home]
    expect(keys!.yawDeg).toBeCloseTo(r.first.keys.yawDeg, 6)
    r.pass(CARD_SWITCH_GLIDE_MS + 500)
    expect(r.hook.result.current.keys).toBe(keys)
    expect(r.hook.result.current.home).toBe(home)
  })

  it('a card on its way is not moved', () => {
    const r = room()
    act(() => { r.hook.result.current.typed('o') })
    act(() => { r.hook.result.current.act({ type: 'grab' }) })
    act(() => { r.hook.result.current.act({ type: 'release', target: { to: 'open' } }) })
    expect(r.hook.result.current.card?.phase).toBe('flying')
    const home = r.hook.result.current.home
    r.pass(KEYS_SWITCH_QUIET_MS + 100)
    r.botAt(yawOf(r.first.keys.at))
    r.go('r2')
    r.pass(AFTER + 2000)
    expect(r.hook.result.current.card?.phase).toBe('flying')
    expect(r.hook.result.current.home).toBe(home)
  })

  it('wait out a key that was pressed in the last second', () => {
    expect(keysSwitchWaitMs(Infinity)).toBe(0)
    expect(keysSwitchWaitMs(KEYS_SWITCH_QUIET_MS)).toBe(0)
    expect(keysSwitchWaitMs(300)).toBe(KEYS_SWITCH_QUIET_MS - 300)
  })
})
