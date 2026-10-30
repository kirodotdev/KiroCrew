import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// The face itself is the avatar component's subject (CrewStateAvatarPackCue
// and friends); here it only has to be a picture of the right size.
vi.mock('../../components/CrewStateAvatar', () => ({
  default: ({ seed, size }: { seed: string; size?: number }) => (
    <img alt="" data-testid="avatar-stub" data-seed={seed} width={size} height={size} />
  ),
}))

import MateEntrance, { ENTRANCE_FACE_PX, type MateEntranceTiming } from './MateEntrance'

/* The first Mate's entrance (#23207): centre stage with its line, the flight
 * into the header pill's face slot, the landing pop, and the reduced-motion
 * path that shows only the line. The host (useMembersPageThread) decides WHEN
 * one runs -- that is pinned in useFirstGreeting.test.tsx -- so these cases
 * drive the component with the timings collapsed. */

// Collapsed, so a case waits on state, never on the real 1.5s hold.
const FAST: MateEntranceTiming = { holdMs: 20, flightSecs: 0.05, popSecs: 0.05 }

const rect = (left: number, top: number, width: number, height = width): DOMRect =>
  ({ x: left, y: top, left, top, right: left + width, bottom: top + height, width, height, toJSON: () => ({}) }) as DOMRect

function mount(opts: { reduceMotion?: boolean; target?: DOMRect | null; text?: string } = {}) {
  const stage = document.createElement('section')
  // The thread column, laid out: 1000 x 800 at the origin, so the stage's
  // centre is (500, 352) and the face's box starts at (425, 277).
  vi.spyOn(stage, 'getBoundingClientRect').mockReturnValue(rect(0, 0, 1000, 800))
  // The header pill's face slot. Detached is fine: only its box is read.
  const target = document.createElement('span')
  if (opts.target !== null) vi.spyOn(target, 'getBoundingClientRect').mockReturnValue(opts.target ?? rect(600, 20, 30))
  const onLanded = vi.fn()
  const onDone = vi.fn()
  const view = render(
    <MateEntrance
      crewmate={{ name: 'mate', label: 'Mate' }}
      slotKey="member-mate"
      text={opts.text ?? "Hi! I'm your first Mate."}
      stageRef={{ current: stage }}
      targetRef={{ current: target }}
      reduceMotion={!!opts.reduceMotion}
      onLanded={onLanded}
      onDone={onDone}
      timing={FAST}
    />,
  )
  return { ...view, target, onLanded, onDone }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('MateEntrance', () => {
  it('opens centre stage: the large face and its one line, the slot not yet landed on', () => {
    const { onLanded, onDone } = mount()
    const root = screen.getByTestId('mate-entrance')
    expect(root).toHaveAttribute('data-phase', 'stage')
    // Above every surface and never a target.
    expect(root.className).toMatch(/\bfixed\b/)
    expect(root.className).toMatch(/pointer-events-none/)
    const face = screen.getByTestId('mate-entrance-face')
    expect(face).toHaveAttribute('aria-hidden', 'true')
    expect(face).toHaveStyle({ width: `${ENTRANCE_FACE_PX}px`, height: `${ENTRANCE_FACE_PX}px` })
    // Centred on the stage: its box starts half a face left and up of the centre.
    expect(face).toHaveStyle({ left: `${500 - ENTRANCE_FACE_PX / 2}px`, top: `${352 - ENTRANCE_FACE_PX / 2}px` })
    expect(face.querySelector('img')).toHaveAttribute('data-seed', 'mate')
    expect(face.querySelector('img')).toHaveAttribute('width', String(ENTRANCE_FACE_PX))
    // The line is a status, so a screen reader hears it once.
    expect(screen.getByRole('status')).toHaveTextContent("Hi! I'm your first Mate.")
    expect(onLanded).not.toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
  })

  it('flies into the header slot, lands there, pops the slot face and ends', async () => {
    const { target, onLanded, onDone } = mount({ target: rect(600, 20, 30) })
    const face = screen.getByTestId('mate-entrance-face')
    await waitFor(() => expect(onLanded).toHaveBeenCalledTimes(1))
    // The flight's end: the face's box is the slot's box. From (425, 277) at
    // 150px to (600, 20) at 30px, as a translate + scale from the top-left.
    expect(face.style.transform).toBe('translate(175px, -257px) scale(0.2)')
    // Landed: the copy is unpainted (the slot's own face is), the ring is up.
    expect(screen.getByTestId('mate-entrance')).toHaveAttribute('data-phase', 'landed')
    expect(face).toHaveStyle({ visibility: 'hidden' })
    expect(screen.getByTestId('mate-entrance-ring')).toBeInTheDocument()
    // The pop runs on the slot's own face and leaves it as it found it.
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    expect(target.style.transform).toBe('')
    expect(onLanded).toHaveBeenCalledTimes(1)
  })

  it('reduced motion: no face, no flight -- the line alone, then it is gone', async () => {
    const { onLanded, onDone } = mount({ reduceMotion: true })
    expect(screen.getByTestId('mate-entrance')).toHaveAttribute('data-reduced-motion', '')
    expect(screen.queryByTestId('mate-entrance-face')).toBeNull()
    expect(screen.getByTestId('mate-entrance-bubble')).toHaveTextContent("Hi! I'm your first Mate.")
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    // Nothing landed, because nothing flew: the host's slot face was never hidden.
    expect(onLanded).not.toHaveBeenCalled()
    expect(screen.queryByTestId('mate-entrance-ring')).toBeNull()
  })

  it('with no slot box to fly to, cuts to landed instead of flying nowhere', async () => {
    // jsdom's own zero rect: the header is not laid out.
    const { onLanded, onDone } = mount({ target: null })
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    expect(onLanded).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('mate-entrance-ring')).toBeNull()
  })

  it('says the line it is given, so the host picks "your first Mate" or the name', () => {
    mount({ text: "Hi! I'm Scout." })
    expect(screen.getByTestId('mate-entrance-bubble')).toHaveTextContent("Hi! I'm Scout.")
  })
})
