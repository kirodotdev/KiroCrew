/**
 * System — a task manager for this install, in Windows 11's shape.
 *
 * Three planes, because the questions they answer are different in kind and mixing
 * them is what made a single flat page unreadable:
 *
 *   Sessions     which session is spending what, right now. One table; every
 *                resource is a COLUMN, focused by sorting, never a view mode.
 *   Performance  how the machine as a whole is doing. Pick a resource, get one
 *                graph and that resource's own numbers. No per-session rows.
 *   Services     the long-lived things that serve sessions without being one:
 *                the gateway process, the shared MCP gateway, embeddings, Slack,
 *                governance.
 *
 * The App-history plane deliberately does NOT live here. Cumulative spend is the
 * Telemetry page's Spend tab, and duplicating it would put the same numbers on
 * two surfaces with two different windows.
 */
import { useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { PageHeader } from '../components/ui'
import UnderlineTabs, { type UnderlineTab } from '../components/UnderlineTabs'
import SessionsTab from './system/SessionsTab'
import PerformanceTab from './system/PerformanceTab'
import ServicesTab from './system/ServicesTab'

import { i18nT } from '../i18n/t'

export type SystemPlane = 'sessions' | 'performance' | 'services'

const VALID_PLANES: ReadonlySet<string> = new Set(['sessions', 'performance', 'services'])

/**
 * A FUNCTION, not a module-level array: the labels are translated, and a
 * module-level constant is evaluated once at import — which would freeze
 * whichever language was active at boot and leave the rail stale after a
 * language switch.
 */
export function buildPlanes(): Array<UnderlineTab<SystemPlane>> {
  return [
    { key: 'sessions', label: i18nT('pages.systemPage.tab_sessions') },
    { key: 'performance', label: i18nT('pages.systemPage.tab_performance') },
    { key: 'services', label: i18nT('pages.systemPage.tab_services') },
  ]
}

/**
 * Stored state that survives plane flips. Each plane stores its own state here
 * before unmounting; on remount the plane reads it back. Kept in a ref so
 * updates never trigger a re-render of the shell.
 */
export interface PlaneState {
  sessions?: SessionsPlaneState
  performance?: PerformancePlaneState
}

export interface SessionsPlaneState {
  sorting: Array<{ id: string; desc: boolean }>
  groupBy: string
  filter: string
  visibility: Record<string, boolean>
}

export interface PerformancePlaneState {
  selected: string
  /** Samples collected so far. Persisted across plane flips because a graph that
   *  restarts empty cannot answer "what just happened?" — the only question it
   *  exists for. */
  history: unknown[]
  /** `dataUpdatedAt` of the last sample already folded into `history`.
   *
   *  This has to travel WITH the history. It is the de-duplication guard, and a
   *  component-local ref resets to 0 on remount while the history survives — so
   *  on the flip back, react-query hands over the still-cached payload, the guard
   *  no longer recognises it, and the last sample is appended a second time. Two
   *  halves of one piece of state; persisting only one is what corrupts it. */
  lastSampleAt: number
}

export default function SystemPage({ embedded }: { embedded?: boolean } = {}) {
  const [params, setParams] = useSearchParams()
  const planeStateRef = useRef<PlaneState>({})

  // Read the plane from ?plane= query param, matching the DeveloperPage ?tab=
  // convention. Fall back to 'sessions' when absent or invalid.
  const rawPlane = params.get('plane')
  const plane: SystemPlane = rawPlane && VALID_PLANES.has(rawPlane) ? (rawPlane as SystemPlane) : 'sessions'

  const setPlane = useCallback((p: SystemPlane) => {
    setParams(prev => {
      const next = new URLSearchParams(prev)
      if (p === 'sessions') next.delete('plane')
      else next.set('plane', p)
      return next
    }, { replace: true })
  }, [setParams])

  return (
    <>
      {!embedded && (
        <PageHeader
          title={i18nT('pages.systemPage.system')}
          subtitle={i18nT('pages.systemPage.live_system_metrics')}
        />
      )}
      <div className={`${embedded ? '' : 'px-6 pb-8'} overflow-y-auto flex-1 min-h-0`}>
        <div className="mb-4">
          <UnderlineTabs<SystemPlane>
            tabs={buildPlanes()}
            value={plane}
            onChange={setPlane}
            ariaLabel={i18nT('pages.systemPage.system_planes')}
            layoutId="system-plane"
          />
        </div>
        {/* Mounted one at a time on purpose: each plane polls on its own interval,
            and keeping all three alive would triple the request rate to sample
            data nobody is looking at. State is persisted in planeStateRef so it
            survives the unmount/remount cycle. */}
        {plane === 'sessions' && <SessionsTab planeStateRef={planeStateRef} />}
        {plane === 'performance' && <PerformanceTab planeStateRef={planeStateRef} />}
        {plane === 'services' && <ServicesTab />}
      </div>
    </>
  )
}
