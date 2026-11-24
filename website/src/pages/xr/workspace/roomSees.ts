/**
 * Whether the room shows a point of a panel's page uncovered: the ray from the eye to that point finds the page's
 * own surface before anything of the room that takes a pointer (a Bot, a handle, a nearer panel). A page drawn by the
 * tab asks it of the control that the dot would snap to (`domAim.ts`): the page itself knows only what IT draws over
 * a control, not what the room draws in front of it.
 */
import { Raycaster, Vector3, type Camera, type Object3D } from 'three'

const ray = new Raycaster()
const eye = new Vector3()
const point = new Vector3()

/** Whether the object or a parent of it takes no pointer (`pointerEvents="none"`) or is not drawn. */
function passes(o: Object3D): boolean {
  for (let n: Object3D | null = o; n; n = n.parent) {
    if (!n.visible) return true
    const events = (n as Object3D & { pointerEvents?: string }).pointerEvents
    if (events === 'none') return true
    if (events !== undefined) return false
  }
  return false
}

/** Whether the room shows the point (x, y) of the page that `page` draws (`css`: the page's size) uncovered. */
export function roomSees(scene: Object3D, camera: Camera, page: Object3D, x: number, y: number, css: readonly [number, number]): boolean {
  camera.getWorldPosition(eye)
  page.localToWorld(point.set(x / css[0] - 0.5, 0.5 - y / css[1], 0))
  const far = point.distanceTo(eye)
  if (far < 1e-6) return false
  ray.set(eye, point.sub(eye).divideScalar(far))
  ray.far = far + 0.01
  for (const hit of ray.intersectObject(scene, true)) {
    if (hit.object === page) return true
    if (!passes(hit.object)) return false
  }
  return false
}
