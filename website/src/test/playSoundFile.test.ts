/**
 * The audio-FILE path — an appearance pack's own cue.
 *
 * Every case here is about failing to SILENCE rather than to an error, because
 * the alternatives are worse in both directions: an exception on a state edge
 * would take the avatar's reaction down with it, and substituting a synthesized
 * preset would report a pack's cue with a sound its author never chose.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

import { playSoundFile } from '../hooks/useNotificationSound'

interface FakeAudio {
  src: string
  volume: number
  onended: (() => void) | null
  onerror: (() => void) | null
  play: ReturnType<typeof vi.fn>
  pause: ReturnType<typeof vi.fn>
}

let built: FakeAudio[] = []
let rejectPlay = false
const original = globalThis.Audio

function install(ctor?: unknown) {
  ;(globalThis as { Audio?: unknown }).Audio = ctor
}

beforeEach(() => {
  built = []
  rejectPlay = false
  install(
    class {
      src: string
      volume = 1
      onended: (() => void) | null = null
      onerror: (() => void) | null = null
      play = vi.fn(() => (rejectPlay ? Promise.reject(new Error('blocked')) : Promise.resolve()))
      pause = vi.fn()
      constructor(src: string) {
        this.src = src
        built.push(this as unknown as FakeAudio)
      }
    },
  )
})

afterEach(() => {
  install(original)
})

describe('playSoundFile', () => {
  it('plays the url at the given volume', () => {
    playSoundFile('/api/appearances/aurora/sound/done', 0.4)
    expect(built).toHaveLength(1)
    expect(built[0].src).toBe('/api/appearances/aurora/sound/done')
    expect(built[0].volume).toBe(0.4)
    expect(built[0].play).toHaveBeenCalled()
  })

  it('clamps the volume rather than letting the element throw', () => {
    // `HTMLMediaElement.volume` throws outside 0..1 instead of clamping, and this
    // number arrives from localStorage — so a hand-edited setting would take the
    // cue down with it.
    playSoundFile('/x', 4)
    expect(built[0].volume).toBe(1)
  })

  it('plays nothing at zero volume, and nothing for an empty url', () => {
    playSoundFile('/x', 0)
    playSoundFile('', 0.5)
    expect(built).toHaveLength(0)
  })

  it('releases the element when it finishes, so a long session accumulates none', () => {
    playSoundFile('/x', 0.5)
    const el = built[0]
    el.onended?.()
    // Both halves: the handlers are what held the element, and the pause is what
    // ends a play still in flight.
    expect(el.onended).toBeNull()
    expect(el.onerror).toBeNull()
    expect(el.pause).toHaveBeenCalled()
  })

  it('releases it on an error too', () => {
    playSoundFile('/x', 0.5)
    built[0].onerror?.()
    expect(built[0].pause).toHaveBeenCalled()
    expect(built[0].onended).toBeNull()
  })

  it('swallows a refused play — the autoplay policy is silence, not a crash', async () => {
    rejectPlay = true
    expect(() => playSoundFile('/x', 0.5)).not.toThrow()
    // The rejection is handled, so no unhandled rejection escapes the turn, and
    // the element is released rather than left buffering.
    await Promise.resolve()
    await Promise.resolve()
    expect(built[0].pause).toHaveBeenCalled()
  })

  it('is a no-op where the environment has no Audio at all', () => {
    install(undefined)
    expect(() => playSoundFile('/x', 0.5)).not.toThrow()
  })

  it('is a no-op when constructing the element throws', () => {
    install(
      class {
        constructor() {
          throw new Error('no media support')
        }
      },
    )
    expect(() => playSoundFile('/x', 0.5)).not.toThrow()
  })
})
