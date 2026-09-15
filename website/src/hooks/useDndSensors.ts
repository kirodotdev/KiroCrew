import {
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  useSensor,
  useSensors,
  type SensorDescriptor,
  type SensorOptions,
} from '@dnd-kit/core'
import { sortableKeyboardCoordinates } from '@dnd-kit/sortable'

/**
 * The touch half of every drag surface in this app: a stationary press-and-hold
 * arms the drag, and moving before it fires HANDS THE GESTURE BACK to the
 * browser.
 *
 * It is shared rather than per-surface because it is not a taste knob. Under a
 * delay constraint, travelling past `tolerance` CANCELS the sensor, which is
 * what lets a finger swipe scroll a list whose rows are all draggable. Raising
 * the tolerance makes a swipe more likely to be captured as a drag instead;
 * lowering the delay makes a scroll more likely to be misread as a pick-up.
 * Both failures look like "the list won't scroll on my phone", and neither is
 * visible on a desktop.
 */
const TOUCH_HOLD_ACTIVATION = { delay: 250, tolerance: 5 } as const

export interface DndSensorOptions {
  /**
   * Mouse activation distance in px. Per surface on purpose: it trades click
   * latency against accidental drags, and the right value depends on what a
   * plain click does (navigate, open, select) and how tightly packed the rows
   * are. This is the ONLY part of the sensor set a surface should be choosing.
   */
  distance: number
  /**
   * Add keyboard dragging with the sortable coordinate getter. Only meaningful
   * for a surface backed by `@dnd-kit/sortable`: the getter walks the sortable
   * ring, so a non-sortable drag (filing something into a folder) has nothing
   * for it to move between and should leave this off.
   */
  keyboard?: boolean
}

/**
 * The sensor set for a dnd-kit surface: mouse and touch as SEPARATE sensors,
 * plus optional keyboard.
 *
 * Splitting mouse from touch is the whole point, and it is a bug fix rather
 * than a preference. A single `PointerSensor` with a distance constraint
 * swallows touch swipes on WebKit: past its activation distance
 * `AbstractPointerSensor.handleMove` calls `preventDefault()` on every
 * subsequent move, and dnd-kit installs a NON-PASSIVE window `touchmove`
 * listener precisely so those calls take effect ("This is required for iOS
 * Safari", `TouchSensor.setup`). So a swipe that begins on a draggable row
 * cannot pan the list. Chromium ignores `preventDefault()` on `pointermove`
 * for panning, which is why the defect shows up only on WebKit -- and only for
 * gestures that start ON a row, since the same gesture starting in the gap
 * between rows has no sensor attached and scrolls normally. A separate
 * `TouchSensor` with a DELAY constraint inverts the contention: movement
 * cancels the sensor instead of being consumed by it.
 *
 * This lived in three copies -- the Apps nav rail, the artifact library, and
 * the chat sidebar -- each with its own retelling of the paragraph above, and
 * the three had already drifted to different mouse distances while agreeing on
 * the touch half. The reasoning is what needed one owner: a copy that keeps the
 * code and loses the explanation is the one that gets "simplified" back to a
 * single PointerSensor.
 *
 * The descriptor array is rebuilt on every render, exactly as it was at the
 * three call sites before this hook existed: `useSensor` memoises on its
 * options object, and that object is a literal. Do not build anything on its
 * identity.
 */
export function useDndSensors({ distance, keyboard = false }: DndSensorOptions): SensorDescriptor<SensorOptions>[] {
  const mouse = useSensor(MouseSensor, { activationConstraint: { distance } })
  const touch = useSensor(TouchSensor, { activationConstraint: TOUCH_HOLD_ACTIVATION })
  // Called unconditionally -- hooks cannot be skipped - and passed as `null`
  // when unwanted. `useSensors` filters null descriptors itself, so this is the
  // library's own supported shape for an optional sensor.
  const keyboardSensor = useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  return useSensors(mouse, touch, keyboard ? keyboardSensor : null)
}
