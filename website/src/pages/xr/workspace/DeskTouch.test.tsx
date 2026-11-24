import { render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DeskTouch } from './DeskTouch'
import { deskHands, type SetupHand } from './deskSetup'
import { touchTargets } from './deskTouch'
import { pinchRoute } from './pinchRoute'

// The frames are the test's own: each call of `frame.run` is one frame of the room.
const frame = vi.hoisted(() => ({ run: () => {} }))
vi.mock('@react-three/fiber', () => ({ useFrame: (run: () => void) => { frame.run = run } }))

const FRAME = 1000 / 90
/** A pointing right hand whose index tip is `h` over a desk at height 0, over the tile at the room's middle. */
const hand = (h: number): SetupHand => {
  const points: [number, number, number][] = [[0, h + 0.05, 0.12], [0, h + 0.05, 0.08], [0.03, h + 0.03, 0.03], [0, h, 0], [0.02, h + 0.04, 0.05], [0.04, h + 0.04, 0.05], [0.05, h + 0.04, 0.06]]
  return { wrist: points[0], points }
}

afterEach(() => {
  vi.restoreAllMocks()
  touchTargets.clear()
  deskHands.left = deskHands.right = null
  pinchRoute.deskY = null
})

describe('a finger\'s touch on the desk, as the room hears it', () => {
  it('says its down before its press, so a wait for the person\'s next press ends at a finger too', () => {
    let now = 0
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    const heard: string[] = []
    touchTargets.set('tile:a', { center: [0, 0, 0], radiusM: 0.04, press: () => heard.push('press'), box: { halfM: 0.04, heightM: 0.01, yawDeg: 0 } })
    pinchRoute.deskY = 0
    render(<DeskTouch onDown={() => heard.push('down')} />)
    const heights = [...Array.from({ length: 30 }, () => 0.06), ...Array.from({ length: 12 }, () => 0.008), ...Array.from({ length: 4 }, () => 0.05)]
    for (const h of heights) {
      deskHands.right = hand(h)
      frame.run()
      now += FRAME
    }
    expect(heard).toEqual(['down', 'press'])
  })
})
