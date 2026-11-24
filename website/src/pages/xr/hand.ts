/**
 * The left-hand entry point (an experiment). The strip shows only while the
 * palm is turned to the face AND the person looks at it, so a hand that types,
 * holds a cup or rests on the desk never opens it.
 *
 * Plain vector maths on tuples, so the rule is tested without a device.
 */
export type V3 = readonly [number, number, number]

const sub = (a: V3, b: V3): V3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
const dot = (a: V3, b: V3): number => a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
const cross = (a: V3, b: V3): V3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
const norm = (a: V3): V3 => {
  const l = Math.hypot(a[0], a[1], a[2]) || 1
  return [a[0] / l, a[1] / l, a[2] / l]
}

export interface PalmJoints {
  wrist: V3
  /** `index-finger-metacarpal` */
  index: V3
  /** `pinky-finger-metacarpal` */
  pinky: V3
}

/**
 * The direction the palm faces. On a left hand held palm-to-face with the
 * fingers up, the index finger is on the person's left and the little finger
 * on their right, so index x pinky points AWAY from the face: the palm
 * normal is its opposite. A right hand mirrors that.
 */
export function palmNormal(j: PalmJoints, handedness: 'left' | 'right'): V3 {
  const n = norm(cross(sub(j.index, j.wrist), sub(j.pinky, j.wrist)))
  return handedness === 'left' ? [-n[0], -n[1], -n[2]] : n
}

/** Open the strip above these, close it below the lower ones: no flicker at the edge. */
export const PALM_OPEN = { facing: 0.6, gaze: 0.9 }
export const PALM_CLOSE = { facing: 0.4, gaze: 0.8 }

export interface PalmView {
  /** 1 = the palm points straight at the eyes. */
  facing: number
  /** 1 = the hand is at the centre of the view. */
  gaze: number
}

export function palmView(j: PalmJoints, handedness: 'left' | 'right', head: V3, forward: V3): PalmView {
  const palm: V3 = [(j.wrist[0] + j.index[0] + j.pinky[0]) / 3, (j.wrist[1] + j.index[1] + j.pinky[1]) / 3, (j.wrist[2] + j.index[2] + j.pinky[2]) / 3]
  const toHead = norm(sub(head, palm))
  const toPalm = norm(sub(palm, head))
  return { facing: dot(palmNormal(j, handedness), toHead), gaze: dot(norm(forward), toPalm) }
}

/** Whether the strip shows, given whether it showed a frame ago. */
export function stripShown(view: PalmView | null, was: boolean): boolean {
  if (!view) return false
  const t = was ? PALM_CLOSE : PALM_OPEN
  return view.facing >= t.facing && view.gaze >= t.gaze
}

/** The hand tools, in order: go to the next ask, the pages that are put away, the whole land. No approval is among them, by design. */
export const HAND_TOOLS = ['next', 'shelf', 'map'] as const
export type HandTool = (typeof HAND_TOOLS)[number]
