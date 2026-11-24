/**
 * Every page, card and label of `/xr`, drawn the framework way: one plane for each surface, with its canvas as the
 * picture on it.
 *
 * A surface (`sceneParts.ts`) is data: a canvas the scene paints, the places on it a hand can press, and where it
 * stands (`at`, a plain transform in the seat's space that is not drawn). The scene's rules say where: at reading
 * distance, facing the head, above its Bot. This file draws what they say.
 *
 * Text stays on a canvas because only the browser's own text engine can show all twelve languages of the dashboard
 * with the fonts the person's system has. A text part of the framework needs a font file with every glyph, and the
 * dashboard ships none for Chinese, Japanese, Korean, Hindi or Bengali.
 */
import { useFrame } from '@react-three/fiber'
import { useLayoutEffect, useRef, useState } from 'react'
import { DoubleSide, type Intersection, type Mesh, type MeshBasicMaterial } from 'three'
import { shown } from './Marks'
import { hitAt } from './painter'
import type { XrScene } from './scene'
import type { Surface } from './sceneParts'

function Panel({ s }: { s: Surface }) {
  const mesh = useRef<Mesh>(null)

  // A surface with gaps (the choice cards stand apart): a ray that goes between two cards goes on to what is behind.
  useLayoutEffect(() => {
    const m = mesh.current
    if (!m || !s.gaps) return
    const whole = m.raycast.bind(m)
    m.raycast = (caster, out) => {
      const met: Intersection[] = []
      whole(caster, met)
      for (const one of met) if (one.uv && hitAt(s.hits, ...s.px(one.uv.x, one.uv.y))) out.push(one)
    }
  }, [s])

  useFrame(() => {
    const m = mesh.current
    if (!m) return
    m.visible = shown(s.at)
    // What a pointer of the framework can be on, and which comes first where two overlap, is the scene's to say.
    m.pointerEvents = m.visible ? s.at.pointerEvents : 'none'
    m.pointerEventsOrder = s.at.pointerEventsOrder
    if (!m.visible) return
    m.matrix.copy(s.at.matrixWorld)
    m.matrixWorldNeedsUpdate = true
    m.renderOrder = s.at.renderOrder
    ;(m.material as MeshBasicMaterial).opacity = s.opacity
  })

  return (
    <mesh ref={mesh} matrixAutoUpdate={false} visible={false} userData={{ surface: s }}>
      <planeGeometry args={[s.width, s.height]} />
      <meshBasicMaterial map={s.tex} transparent depthWrite={false} side={DoubleSide} />
    </mesh>
  )
}

/** One plane for each surface the scene has now. Pages come and go; the list follows. */
export function Panels({ scene }: { scene: XrScene }) {
  const [list, setList] = useState<readonly Surface[]>([])
  const key = useRef('')
  useFrame(() => {
    const now = scene.panels()
    const k = now.map(s => s.key).join('|')
    if (k !== key.current) { key.current = k; setList(now) }
  })
  return <>{list.map(s => <Panel key={s.key} s={s} />)}</>
}
