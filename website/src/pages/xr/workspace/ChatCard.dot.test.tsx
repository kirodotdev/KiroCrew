import { act, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { Vector3 } from 'three'

// The framework is stood in for: the card and its keys are drawn as they are, and the test moves the dot.
vi.mock('@react-three/fiber', () => ({
  useFrame: () => undefined,
  useThree: (pick: (s: unknown) => unknown) => pick({ camera: { getWorldPosition: (v: Vector3) => v.set(0, 1.6, 0), getWorldDirection: (v: Vector3) => v.set(0, 0, -1) } }),
}))
vi.mock('@react-three/xr', () => ({ useXR: (pick: (s: unknown) => unknown) => pick({ session: null }) }))

import { ChatCard } from './ChatCard'
import { CARD_DOT_AWAY_MS, CARD_SWITCH_GLIDE_MS, KEYS_SWITCH_SETTLE_MS, useChatCard, type ChatCardState } from './useChatCard'

/** A Bot's feet on the floor, so many degrees to the right of his head, 4 m away. */
const feetAt = (yawDeg: number): [number, number, number] => { const r = (yawDeg * Math.PI) / 180; return [Math.sin(r) * 4, 0, -Math.cos(r) * 4] }
const yawOf = (at: readonly number[]) => (Math.atan2(at[0], -at[2]) * 180) / Math.PI
const AFTER = KEYS_SWITCH_SETTLE_MS + CARD_SWITCH_GLIDE_MS + 50
const HEAD = new Vector3(0, 1.6, 0)

/**
 * The dot goes from one mesh to another (`null`: to nothing, from nothing). The page's own enter and leave follow
 * the rule of the room's pointer library (`computeEnterLeave`): what is entered is the mesh that is hit and every
 * group above it; a group is left only when no mesh under it is hit any more.
 */
/** An event of one pointer, named by its id, to or from nothing (the test's document has no `PointerEvent` of its own). */
const pointerEvent = (type: 'pointerover' | 'pointerout', pointerId: number): Event => Object.assign(new MouseEvent(type, { bubbles: true, cancelable: true, relatedTarget: null }), { pointerId })

const dotGoes = (from: Element | null, to: Element | null) => {
  if (from) fireEvent.pointerOut(from, { relatedTarget: to })
  else if (to) fireEvent.pointerOver(to)
}

describe('his dot on the card and its keys, as the two components tell it (M154, M155)', () => {
  const w = window as unknown as { SpeechRecognition?: unknown }
  afterEach(() => { delete w.SpeechRecognition })

  /** The card and its keys drawn in room `r1`, a Bot that asks under them, and the hook they tell. */
  const drawn = (speech = false) => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance', 'Date'] })
    onTestFinished(() => { vi.useRealTimers() })
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    onTestFinished(() => { vi.restoreAllMocks() })
    if (speech) w.SpeechRecognition = class { lang = ''; onstart = null; onresult = null; onerror = null; onend = null; start() {} stop() {} abort() {} }
    const world = { bots: [] as unknown[], bodies: [] as unknown[] }
    const now: { state: ChatCardState | null } = { state: null }
    function Room({ room }: { room: string }) {
      const state = useChatCard({ head: () => HEAD, lookYawDeg: () => 0, desk: () => null, bots: () => world.bots as never, bodies: () => world.bodies as never, openPage: () => {}, say: () => {}, room })
      now.state = state
      return <ChatCard state={state} head={HEAD} labelOf={s => s} hushed={false} />
    }
    const page = render(<Room room="r1" />)
    const state = () => now.state!
    act(() => { state().open() })
    const first = { keys: state().keys!, home: state().home! }
    /** A Bot that asks comes to stand under the card and its keys, where they stand now. */
    const botUnder = () => {
      world.bots = [{ slug: 'atlas', state: 'needs' }]
      world.bodies = [{ slug: 'atlas', feet: feetAt(yawOf(state().keys!.at)), top: [0, 1.2, -1.6] }]
    }
    botUnder()
    const mesh = (name: string) => page.container.querySelector(`mesh[name="${name}"]`)
    /** The keys' meshes that take a press, after the board: one for each key. */
    const key = (i: number) => { const all = [...mesh('chat-keys-board')!.parentElement!.children]; return all.slice(all.indexOf(mesh('chat-keys-board')!) + 1)[i] }
    const go = (room: string) => page.rerender(<Room room={room} />)
    const pass = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })
    const still = () => { expect(state().keys).toBe(first.keys); expect(state().home).toBe(first.home) }
    const glided = () => {
      expect(state().keys!.yawDeg).toBeCloseTo(-first.keys.yawDeg, 6)
      expect(yawOf(state().home!)).toBeCloseTo(state().keys!.yawDeg, 3)
    }
    /** The dot leaves `from` for nothing: nothing moves for 400 ms, then both glide. */
    const leavesAndGlides = (from: Element) => {
      dotGoes(from, null)
      pass(CARD_DOT_AWAY_MS - 10)
      still()
      pass(10 + CARD_SWITCH_GLIDE_MS + 50)
      glided()
    }
    return { state, first, world, botUnder, mesh, key, go, pass, still, glided, leavesAndGlides }
  }

  it('draws a board and more than one key beside it under one group, and the card\'s marks under the card\'s', () => {
    const r = drawn(true)
    expect(r.mesh('chat-keys-board')!.parentElement).toBe(r.key(0).parentElement)
    expect(r.key(0)).not.toBe(r.key(1))
    for (const name of ['chat-card-close', 'chat-card-mic', 'chat-card-lang']) expect(r.mesh('chat-card')!.parentElement!.contains(r.mesh(name))).toBe(true)
  })

  it('his dot comes straight onto a key: nothing moves at a room switch; it leaves, and they glide 400 ms later', () => {
    const r = drawn()
    dotGoes(null, r.key(3))
    r.go('r2')
    r.pass(AFTER + 3000)
    r.still()
    r.leavesAndGlides(r.key(3))
  })

  it('his dot goes from the board\'s margin onto a key, to the next key and back to the margin: it is on the keys all the time', () => {
    const r = drawn()
    const board = r.mesh('chat-keys-board')!
    dotGoes(null, board)
    r.go('r2')
    r.pass(AFTER)
    for (const [from, to] of [[board, r.key(0)], [r.key(0), r.key(1)], [r.key(1), board]]) {
      dotGoes(from, to)
      r.pass(CARD_DOT_AWAY_MS + 1000)
      r.still()
    }
    r.leavesAndGlides(board)
  })

  it('his dot goes over the card\'s face, its close mark, the microphone\'s mark and the language\'s: it is on the card all the time', () => {
    const r = drawn(true)
    const [face, close, mic, lang] = ['chat-card', 'chat-card-close', 'chat-card-mic', 'chat-card-lang'].map(n => r.mesh(n)!)
    dotGoes(null, close)
    r.go('r2')
    r.pass(AFTER)
    for (const [from, to] of [[close, face], [face, mic], [mic, lang], [lang, face]]) {
      dotGoes(from, to)
      r.pass(CARD_DOT_AWAY_MS + 1000)
      r.still()
    }
    r.leavesAndGlides(face)
  })

  it('his dot goes from the card onto a key: the wait starts anew when it leaves the keys', () => {
    const r = drawn()
    dotGoes(null, r.mesh('chat-card'))
    r.go('r2')
    r.pass(AFTER)
    dotGoes(r.mesh('chat-card'), r.key(2))
    r.pass(CARD_DOT_AWAY_MS + 1000)
    r.still()
    r.leavesAndGlides(r.key(2))
  })

  it('his dot is on neither: they glide once, with no wait for it', () => {
    const r = drawn()
    r.go('r2')
    r.pass(AFTER)
    r.glided()
    const [keys, home] = [r.state().keys, r.state().home]
    r.pass(5000)
    expect(r.state().keys).toBe(keys)
    expect(r.state().home).toBe(home)
  })

  it('the microphone\'s mark goes away under his dot (the microphone turns `none`): the card glides when the dot leaves its face', () => {
    const r = drawn(true)
    dotGoes(null, r.mesh('chat-card-mic'))
    r.go('r2')
    r.pass(AFTER)
    act(() => { r.state().act({ type: 'mic', state: 'none' }) })
    expect(r.mesh('chat-card-mic')).toBeNull()
    r.pass(5000)
    r.still()
    // The mark sent no leave. The face behind it is hit now; when the dot leaves that, the card's group is left.
    r.leavesAndGlides(r.mesh('chat-card')!)
  })

  it('the card is closed under his dot and a new one comes out: the new card has no dot on it', () => {
    const r = drawn()
    dotGoes(null, r.mesh('chat-card-close'))
    act(() => { r.state().close() })
    expect(r.mesh('chat-card')).toBeNull()
    r.world.bots = []
    act(() => { r.state().open() })
    r.botUnder()
    const [keys, home] = [r.state().keys!, r.state().home!]
    r.go('r2')
    r.pass(AFTER)
    expect(r.state().keys).not.toBe(keys)
    expect(r.state().home).not.toBe(home)
  })

  it('a card that is closed while a room switch waits for his dot lets the wait go: nothing is left to glide', () => {
    const r = drawn()
    dotGoes(null, r.mesh('chat-card'))
    r.go('r2')
    r.pass(AFTER)
    act(() => { r.state().close() })
    r.pass(CARD_DOT_AWAY_MS + CARD_SWITCH_GLIDE_MS + 1000)
    expect(r.state().card).toBeNull()
    expect(r.state().keys).toBeNull()
  })

  it('the keys are put away under his dot and come back: they have no dot on them', () => {
    const r = drawn()
    dotGoes(null, r.key(5))
    act(() => { r.state().keysAway(true) })
    expect(r.mesh('chat-keys-board')).toBeNull()
    r.go('r2')
    r.pass(AFTER)
    act(() => { r.state().keysAway(false) })
    expect(yawOf(r.state().home!)).toBeCloseTo(-yawOf(r.first.home), 3)
  })

  it('two pointers on the keys, each on a key of its own: one leaves and nothing moves; the other leaves and they glide (M170)', () => {
    const r = drawn()
    // His dot is pointer 1, a raised hand's ray pointer 2: the group tells the hook each by its id.
    fireEvent(r.key(3), pointerEvent('pointerover', 1))
    r.go('r2')
    r.pass(AFTER)
    fireEvent(r.key(8), pointerEvent('pointerover', 2))
    fireEvent(r.key(3), pointerEvent('pointerout', 1))
    r.pass(CARD_DOT_AWAY_MS + 3000)
    r.still()
    fireEvent(r.key(8), pointerEvent('pointerout', 2))
    r.pass(CARD_DOT_AWAY_MS - 10)
    r.still()
    r.pass(10 + CARD_SWITCH_GLIDE_MS + 50)
    r.glided()
  })

  it('two pointers on the card, his dot on its face and a hand\'s ray on its close mark: one leaves and nothing moves', () => {
    const r = drawn()
    fireEvent(r.mesh('chat-card')!, pointerEvent('pointerover', 1))
    fireEvent(r.mesh('chat-card-close')!, pointerEvent('pointerover', 2))
    r.go('r2')
    r.pass(AFTER)
    fireEvent(r.mesh('chat-card')!, pointerEvent('pointerout', 1))
    r.pass(CARD_DOT_AWAY_MS + 3000)
    r.still()
    fireEvent(r.mesh('chat-card-close')!, pointerEvent('pointerout', 2))
    r.pass(CARD_DOT_AWAY_MS + CARD_SWITCH_GLIDE_MS + 50)
    r.glided()
  })

  it('his dot on the card and a hand\'s ray on a key: the dot leaves the card, and the ray still holds them', () => {
    const r = drawn()
    fireEvent(r.mesh('chat-card')!, pointerEvent('pointerover', 1))
    fireEvent(r.key(2), pointerEvent('pointerover', 2))
    r.go('r2')
    r.pass(AFTER)
    fireEvent(r.mesh('chat-card')!, pointerEvent('pointerout', 1))
    r.pass(CARD_DOT_AWAY_MS + 3000)
    r.still()
    fireEvent(r.key(2), pointerEvent('pointerout', 2))
    r.pass(CARD_DOT_AWAY_MS + CARD_SWITCH_GLIDE_MS + 50)
    r.glided()
  })
})
