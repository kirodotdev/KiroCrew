/**
 * Where the cursor grips the pet while dragging: dead centre, always.
 *
 * This used to be an art-relative offset near the ghost's front, mirrored with the
 * body — which meant the pet hung off the pointer at an angle that changed with
 * facing, and the drag needed its own "held" drawing to look deliberate. With one
 * body and a centred grip the ghost simply follows the cursor, which is also the
 * only sane answer for a custom pack whose art we know nothing about.
 */
import { PET_W, PET_H } from './constants'

export type GripInput = { flipped?: boolean; custom?: boolean }

export function dragGrip(_opts: GripInput = {}): { x: number; y: number } {
  return { x: PET_W / 2, y: PET_H / 2 }
}
