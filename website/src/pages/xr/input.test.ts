import { describe, expect, it } from 'vitest'
import { HAND_TOOLS, PALM_CLOSE, PALM_OPEN, palmNormal, palmView, stripShown, type PalmJoints } from './hand'
import { AIM_REST_MS, DISARM_MS, HOLD_MS, PressGuard } from './input'

const plain = { id: 'bot:atlas' }
const approve = { id: 'hub/approve:1', guarded: true }

/** Aim at a target, rest, press, hold, release: the press that is meant to count. */
function fullPress(g: PressGuard, source = 'hand-r', t0 = 0) {
  g.hover(source, approve.id, t0)
  g.down(source, approve, t0 + AIM_REST_MS)
  return g.up(source, approve.id, t0 + AIM_REST_MS + HOLD_MS)
}

describe('press rules', () => {
  it('a plain target commits on press and release by one source', () => {
    const g = new PressGuard()
    g.down('mouse', plain, 0)
    expect(g.up('mouse', plain.id, 10)).toEqual({ commit: 'bot:atlas' })
  })

  it('a release with no completed select never commits', () => {
    const g = new PressGuard()
    g.down('hand-r', plain, 0)
    expect(g.up('hand-r', plain.id, 10, false)).toEqual({ refused: 'not-completed' })
  })

  it('the release must come from the source that pressed', () => {
    const g = new PressGuard()
    g.down('hand-r', plain, 0)
    expect(g.up('hand-l', plain.id, 10)).toEqual({ refused: 'other-source' })
  })

  it('the release must be on the target that was pressed', () => {
    const g = new PressGuard()
    g.down('mouse', plain, 0)
    expect(g.up('mouse', 'bot:juno', 10)).toEqual({ refused: 'other-target' })
  })

  it('a guarded target commits after aim, rest, hold and release', () => {
    expect(fullPress(new PressGuard())).toEqual({ commit: approve.id })
  })

  it('a guarded press needs the aim to rest first: a hand that was already closed starts nothing', () => {
    const g = new PressGuard()
    g.hover('hand-r', approve.id, 0)
    g.down('hand-r', approve, AIM_REST_MS - 1)
    expect(g.up('hand-r', approve.id, AIM_REST_MS + HOLD_MS + 50)).toEqual({ refused: 'no-aim' })
  })

  it('a quick pinch on a guarded target does nothing', () => {
    const g = new PressGuard()
    g.hover('hand-r', approve.id, 0)
    g.down('hand-r', approve, AIM_REST_MS)
    expect(g.up('hand-r', approve.id, AIM_REST_MS + HOLD_MS - 1)).toEqual({ refused: 'too-short' })
  })

  it('a card that just appeared or changed is disarmed for a moment', () => {
    const g = new PressGuard()
    g.hover('hand-r', approve.id, 0)
    g.arm(approve.id, AIM_REST_MS)
    g.down('hand-r', approve, AIM_REST_MS + DISARM_MS - 1)
    expect(g.up('hand-r', approve.id, AIM_REST_MS + DISARM_MS + HOLD_MS)).toEqual({ refused: 'disarmed' })
  })

  it('a card that changes under a held press cancels it', () => {
    const g = new PressGuard()
    g.hover('hand-r', approve.id, 0)
    g.down('hand-r', approve, AIM_REST_MS)
    g.arm(approve.id, AIM_REST_MS + 100)
    expect(g.up('hand-r', approve.id, AIM_REST_MS + HOLD_MS)).toEqual({ refused: 'spent' })
    expect(g.lastCancel).toBe('card-changed')
  })

  it('the aim leaving a guarded target cancels the hold', () => {
    const g = new PressGuard()
    g.hover('hand-r', approve.id, 0)
    g.down('hand-r', approve, AIM_REST_MS)
    g.hover('hand-r', '', AIM_REST_MS + 100)
    g.hover('hand-r', approve.id, AIM_REST_MS + 200)
    expect(g.up('hand-r', approve.id, AIM_REST_MS + HOLD_MS)).toEqual({ refused: 'spent' })
    expect(g.lastCancel).toBe('left-target')
  })

  it('a second hand pressing cancels the first press', () => {
    const g = new PressGuard()
    g.hover('hand-r', approve.id, 0)
    g.down('hand-r', approve, AIM_REST_MS)
    g.down('hand-l', plain, AIM_REST_MS + 100)
    expect(g.up('hand-r', approve.id, AIM_REST_MS + HOLD_MS)).toEqual({ refused: 'spent' })
    expect(g.lastCancel).toBe('second-source')
  })

  it.each(['blur', 'hidden', 'tracking-lost', 'session-end', 'system', 'scrolled'] as const)('%s cancels a held press', reason => {
    const g = new PressGuard()
    g.hover('hand-r', approve.id, 0)
    g.down('hand-r', approve, AIM_REST_MS)
    g.cancel(reason)
    expect(g.up('hand-r', approve.id, AIM_REST_MS + HOLD_MS)).toEqual({ refused: 'spent' })
  })

  it('a cancelled press is spent: only a fresh press can commit', () => {
    const g = new PressGuard()
    g.hover('hand-r', approve.id, 0)
    g.down('hand-r', approve, AIM_REST_MS)
    g.cancel('blur')
    expect(g.up('hand-r', approve.id, AIM_REST_MS + HOLD_MS)).toEqual({ refused: 'spent' })
    expect(g.up('hand-r', approve.id, AIM_REST_MS + HOLD_MS + 1)).toEqual({ refused: 'no-press' })
    g.down('hand-r', approve, 5000)
    expect(g.up('hand-r', approve.id, 5000 + HOLD_MS)).toEqual({ commit: approve.id })
  })

  it('the puck can aim but never completes a guarded press', () => {
    const g = new PressGuard()
    expect(fullPress(g, 'puck')).toEqual({ refused: 'puck' })
    g.down('puck', plain, 9000)
    expect(g.up('puck', plain.id, 9010)).toEqual({ commit: 'bot:atlas' })
  })

  it('shows how far a hold has come', () => {
    const g = new PressGuard()
    g.hover('hand-r', approve.id, 0)
    g.down('hand-r', approve, AIM_REST_MS)
    expect(g.holdProgress(AIM_REST_MS + HOLD_MS / 2)).toBeCloseTo(0.5)
    expect(g.pressed).toBe(approve.id)
  })
})

describe('the hand strip', () => {
  // A real left hand held up, palm to the face, fingers up. Head at the origin looking down -z.
  // The index finger is on the person's left (-x) and the little finger on their right.
  const up: PalmJoints = { wrist: [-0.1, -0.2, -0.4], index: [-0.13, -0.12, -0.4], pinky: [-0.07, -0.13, -0.4] }
  // The same hand turned over: the back of the hand faces the person.
  const flip = (j: PalmJoints): PalmJoints => ({ wrist: j.wrist, index: j.pinky, pinky: j.index })
  const head = [0, 0, 0] as const
  const at = [-0.1, -0.15, -0.4] as const

  it('knows which way a palm faces, for either hand', () => {
    expect(palmNormal(up, 'left')[2]).toBeCloseTo(-palmNormal(up, 'right')[2])
  })

  it('shows only when the palm is turned to the face and looked at', () => {
    const facing = up
    // The palm of that left hand points at the face (+z), not away from it.
    expect(palmNormal(up, 'left')[2]).toBeGreaterThan(0.9)
    const view = palmView(facing, 'left', head, at)
    expect(view.facing).toBeGreaterThan(PALM_OPEN.facing)
    expect(stripShown(view, false)).toBe(true)
    expect(stripShown(palmView(flip(facing), 'left', head, at), false)).toBe(false)
    expect(stripShown(palmView(facing, 'left', head, [1, 0, 0]), false)).toBe(false)
  })

  it('does not flicker at the edge, and hides when tracking is lost', () => {
    const edge = { facing: (PALM_OPEN.facing + PALM_CLOSE.facing) / 2, gaze: 1 }
    expect(stripShown(edge, false)).toBe(false)
    expect(stripShown(edge, true)).toBe(true)
    expect(stripShown(null, true)).toBe(false)
  })

  it('carries three tools and no approval', () => {
    expect(HAND_TOOLS).toEqual(['next', 'shelf', 'map'])
  })
})
