// A mark for the rows only: a small magenta ball at a place in the room that a row names, and where the build's own
// camera puts it in the picture. A row finds the ball's pixels in a still and so reads which projection the picture
// follows: the build's camera or the row's own (D107 2). Mounted only while a probe reads the room.
import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useRef } from 'react'
import { Vector3, type Mesh } from 'three'

/** What a row sets (`at`, a place in the room in metres) and reads back (`ndc`, by the camera the probe reads). */
export const roomMark = { at: null as [number, number, number] | null, ndc: null as [number, number] | null }

const MARK_COLOR = '#ff00ff'
const MARK_RADIUS_M = 0.008
const AT = new Vector3()

export function RoomMark() {
  const camera = useThree(s => s.camera)
  const ball = useRef<Mesh>(null)
  useEffect(() => {
    const hooked = window as unknown as { __xrRoomMark?: typeof roomMark }
    hooked.__xrRoomMark = roomMark
    return () => { delete hooked.__xrRoomMark; roomMark.at = null; roomMark.ndc = null }
  }, [])
  useFrame(() => {
    const m = ball.current
    if (!m) return
    const at = roomMark.at
    m.visible = at !== null
    if (!at) { roomMark.ndc = null; return }
    AT.set(at[0], at[1], at[2])
    m.position.copy(m.parent ? m.parent.worldToLocal(AT.clone()) : AT)
    const p = AT.clone().project(camera)
    roomMark.ndc = [p.x, p.y]
  })
  return (
    <mesh ref={ball} visible={false} renderOrder={10000}>
      <sphereGeometry args={[MARK_RADIUS_M, 16, 12]} />
      <meshBasicMaterial color={MARK_COLOR} depthTest={false} depthWrite={false} toneMapped={false} />
    </mesh>
  )
}
