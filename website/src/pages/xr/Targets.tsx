/**
 * What a pointer of the framework can be on that is not a surface: each Bot, each thing, each marble, the bin,
 * and the ground. Each is a plain shape that is not drawn. The framework's pointers meet them as they meet any
 * mesh, and their events come up to the stage (`XrStage.tsx`), which tells the scene what was met.
 *
 * The scene says where each one is and how big (`scene.targets()`): none is smaller than the least angle a hand
 * can aim at.
 */
import { useFrame } from '@react-three/fiber'
import { useRef, useState } from 'react'
import { DoubleSide, Vector3, type Intersection, type Mesh, type Object3D } from 'three'
import { At } from './Marks'
import type { XrScene } from './scene'
import { ORDER } from './sceneBase'

const MIDDLE = new Vector3()
const EYE = new Vector3()
const ALONG = new Vector3()
/** How far off the ray's line the middle of a target is, as an angle at the ray's start. */
const off = (target: Object3D): number => target.getWorldPosition(MIDDLE).sub(EYE).angleTo(ALONG)

/**
 * Which of two parts in a ray's way the pointer takes: the framework asks this of every pointer. The kind that comes
 * first wins (`ORDER`), then the nearer. One rule is the page's own: a thing that stands next to a Bot is small and
 * close to it, so of a Bot and a thing of the land in line, the one whose middle the ray passes nearer to is meant.
 */
export function inLine(a: Intersection, orderA = 0, b: Intersection, orderB = 0): number {
  if (orderA !== orderB) return orderB - orderA
  if (orderA === ORDER.land && a.object !== b.object && a.object.userData.pick && b.object.userData.pick) {
    // Both places are on the pointer's ray, so the line through them is the ray, and it starts `distance` back from each.
    ALONG.copy(b.point).sub(a.point)
    if (ALONG.lengthSq() > 1e-12) {
      ALONG.normalize().multiplyScalar(b.distance >= a.distance ? 1 : -1)
      EYE.copy(a.point).addScaledVector(ALONG, -a.distance)
      return off(a.object) - off(b.object)
    }
  }
  return a.distance - b.distance
}

export function Targets({ scene }: { scene: XrScene }) {
  const [ids, setIds] = useState<readonly string[]>([])
  const key = useRef('')
  const balls = useRef(new Map<string, Mesh>())
  const ground = useRef<Mesh>(null)

  // Before the framework moves its pointers, so each one meets the targets where they are now.
  useFrame(() => {
    const list = scene.targets()
    const now = list.map(t => t.id).join('|')
    if (now !== key.current) { key.current = now; setIds(list.map(t => t.id)) }
    for (const t of list) {
      const ball = balls.current.get(t.id)
      if (!ball) continue
      ball.position.copy(t.at)
      ball.scale.setScalar(t.radius)
      ball.pointerEventsOrder = t.order
    }
    if (ground.current) ground.current.position.y = scene.groundY
  }, -60)

  return (
    <At at={scene.seat}>
      {ids.map(id => (
        // It has no size until the scene has said where it is.
        <mesh key={id} ref={m => { if (m) balls.current.set(id, m); else balls.current.delete(id) }} userData={{ pick: id }} scale={0} pointerEvents="auto">
          <sphereGeometry args={[1, 12, 8]} />
          <meshBasicMaterial visible={false} />
        </mesh>
      ))}
      <mesh ref={ground} rotation-x={-Math.PI / 2} userData={{ pick: 'land' }} pointerEvents="auto" pointerEventsOrder={ORDER.ground}>
        <planeGeometry args={[160, 160]} />
        <meshBasicMaterial visible={false} side={DoubleSide} />
      </mesh>
    </At>
  )
}
