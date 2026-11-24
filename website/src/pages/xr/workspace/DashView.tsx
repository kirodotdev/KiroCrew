/**
 * `/xr?view=dash&slug=<slug>`: one Bot's 3D dashboard alone in an empty room, from one of a fixed list of cameras
 * (`dashView.ts`). No crew, no Home, no page, no board: the Bot itself is not drawn, only where its feet stand. The
 * layout and its values come by the room's own two reads (`useDash3d.ts`), or the samples with `fixture=1`.
 */
import { Canvas, useThree } from '@react-three/fiber'
import { useQuery } from '@tanstack/react-query'
import { membersRosterQuery } from '../../../api/membersQuery'
import { i18nT } from '../../../i18n/t'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { XR_GL, readPalette } from '../palette'
import { toGl } from '../sceneParts'
import type { CrewProbe } from './Crew'
import { Dash3d, type DashMarks } from './Dash3d'
import { DASH_EYE, DASH_FEET, dashCamera, dashSlug, type DashCamera } from './dashView'
import { useDash3d } from './useDash3d'

/** The Aura's lens, so a still is what the glasses show. */
const FOVY_DEG = 40.7
const NONE = new Set<string>()

/** Puts the camera where the list says and turns it to its point. */
function Aim({ camera }: { camera: DashCamera }) {
  const three = useThree(s => s.camera)
  useEffect(() => {
    three.position.set(...camera.position)
    three.lookAt(...camera.target)
    three.updateMatrixWorld()
  }, [three, camera])
  return null
}

export default function DashView() {
  const [params] = useSearchParams()
  const slug = dashSlug(params.get('slug'))
  const camera = dashCamera(params.get('camera'))
  const fixture = params.get('fixture') === '1'
  const probe = params.get('probe') === '1'
  // The Bot as the room has it (Q4, Q5): its roster `name` is what the dashboard read is keyed on, and it can differ
  // from the slug by more than case (`member_dashboard.py` resolves `member` by the roster's own spelling and then
  // its id, never by the slug); its label is the one the room draws (`model.ts`: `display_name`, else `name`). The
  // roster read is the one the room makes (`membersRosterQuery`). A slug the roster does not hold reads nothing more.
  // With `fixture=1` the sample stands under the slug, and the picture says it is a sample (Q3).
  const roster = useQuery({ ...membersRosterQuery, enabled: !!slug && !fixture })
  const bots = useMemo(() => {
    if (!slug) return []
    if (fixture) return [{ slug, name: slug, label: slug.charAt(0).toUpperCase() + slug.slice(1) }]
    const row = (roster.data ?? []).find(m => m.slug === slug)
    return row ? [{ slug, name: row.name, label: (row.display_name ?? '').trim() || row.name }] : []
  }, [slug, fixture, roster.data])
  const dash = useDash3d(bots, fixture, probe)
  const host = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<Readonly<Record<string, number>> | null>(null)
  useEffect(() => {
    if (!host.current) return
    const pal = readPalette(host.current)
    setState({ idle: XR_GL.dim, working: toGl(pal.accent), needs: toGl(pal.warn), failed: toGl(pal.danger) })
  }, [])
  // The one Bot, where its feet stand: the dashboard at rest stands in front of its body, which is not drawn.
  const crew = useMemo(() => (slug ? [{ slug, feet: DASH_FEET, top: [DASH_FEET[0], DASH_FEET[1] + 0.32, DASH_FEET[2]], widthM: 0.216, seat: false } as unknown as CrewProbe] : []), [slug])
  const marks = useRef<(() => Record<string, DashMarks>) | null>(null)
  useEffect(() => {
    if (!probe) return
    const hooked = window as unknown as { __xrDashView?: { read: () => { camera: string; slug: string | null; dash: Record<string, DashMarks> } } }
    hooked.__xrDashView = { read: () => ({ camera: camera.name, slug, dash: marks.current?.() ?? {} }) }
    return () => { delete hooked.__xrDashView }
  }, [probe, camera, slug])
  return (
    <div ref={host} data-testid="xr-dash-view" style={{ position: 'fixed', inset: 0, background: '#0b0d12' }}>
      <Canvas data-testid="xr-canvas" camera={{ fov: FOVY_DEG, near: 0.05, far: 50, position: [...camera.position] }} gl={{ preserveDrawingBuffer: probe }}>
        <Aim camera={camera} />
        {state && dash.views.length > 0 && (
          <Dash3d
            views={dash.views}
            crew={() => crew}
            block={null}
            boardsUp={NONE}
            say={() => {}}
            still
            stateColors={state}
            report={probe ? read => { marks.current = read } : undefined}
            // Taken from his eye at the room's middle for both taken cameras: the aside one walks round it.
            takenFrom={camera.taken ? DASH_EYE : undefined}
          />
        )}
      </Canvas>
      {fixture && <span className="absolute left-3 top-3 inline-flex items-center gap-1 rounded-full border border-border bg-bg-elevated px-2 py-0.5 text-[12px] text-muted" data-testid="xr-sample">{i18nT('xr.sample_data')}</span>}
    </div>
  )
}
