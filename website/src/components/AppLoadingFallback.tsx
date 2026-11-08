/**
 * AppLoadingFallback — the Suspense fallback AppHost shows while an app's ESM
 * bundle is in flight.
 *
 * Two problems with a single always-on skeleton:
 *
 * 1. FLASH. Apps load from the local gateway, usually in a few hundred ms. A
 *    skeleton painted on the first frame and torn down 150 ms later reads as a
 *    flicker, not as progress. So nothing renders for the first
 *    `APP_LOADING_DELAY_MS`; a load that lands inside that window never shows a
 *    placeholder at all.
 * 2. SHAPE JUMP. Every app got the same page-header-plus-rows placeholder, so an
 *    app laid out as a left rail + composer jumped on mount. An app can now name
 *    its shape in `app.json` as `ui.loading.layout`.
 *
 * The layouts are a CLOSED set drawn here from the dashboard's own skeleton
 * pieces. An app picks a name; it never supplies markup, classes or styles, so
 * the fallback cannot become a way for a not-yet-loaded bundle to paint into the
 * host page. Anything that is not exactly one of the names falls back to "page",
 * which is the skeleton every app had before. The backend parser
 * (`UIConfig.from_dict` in `apps/manifest_sections.py`) applies the same rule, so
 * this check only matters for a manifest that reached the page another way.
 */
import { useEffect, useState, type ReactElement } from 'react'
import { ContentSkeleton, Skeleton } from './ui'
import { useReducedMotion } from '../hooks/useReducedMotion'
import { i18nT } from '../i18n/t'

export const APP_LOADING_LAYOUTS = ['page', 'rail', 'centered', 'split'] as const
export type AppLoadingLayout = (typeof APP_LOADING_LAYOUTS)[number]

/** How long a load may take before any placeholder appears. */
export const APP_LOADING_DELAY_MS = 300

/**
 * The layout a manifest's `ui.loading` value asks for. Total: any input that is
 * not an object whose `layout` is exactly one of `APP_LOADING_LAYOUTS` (case
 * included) yields "page".
 */
export function resolveAppLoadingLayout(loading: unknown): AppLoadingLayout {
  if (!loading || typeof loading !== 'object' || Array.isArray(loading)) return 'page'
  // Own property only: a layout inherited through the prototype chain is not
  // something the manifest declared.
  if (!Object.prototype.hasOwnProperty.call(loading, 'layout')) return 'page'
  const layout = (loading as { layout?: unknown }).layout
  return typeof layout === 'string' && (APP_LOADING_LAYOUTS as readonly string[]).includes(layout)
    ? (layout as AppLoadingLayout)
    : 'page'
}

/** False until `delayMs` has passed since mount, then true. The timer is cleared
 *  on unmount, so a fallback torn down early never sets state afterwards. */
export function useDelayElapsed(delayMs: number): boolean {
  const [elapsed, setElapsed] = useState(delayMs <= 0)
  useEffect(() => {
    if (delayMs <= 0) return
    const id = window.setTimeout(() => setElapsed(true), delayMs)
    return () => window.clearTimeout(id)
  }, [delayMs])
  return elapsed
}

function LoadingLabel({ appName, className = '' }: { appName: string; className?: string }) {
  return <p className={`text-[12px] text-muted ${className}`}>{i18nT('components.appHost.loading')} {appName}…</p>
}

/** Today's skeleton, unchanged: title bar, subtitle bar, six content rows. */
function PageShape({ appName }: { appName: string }) {
  return (
    <div className="px-6 pt-6 pb-8" data-testid="app-loading-shape-page">
      <div className="skeleton h-7 w-48 rounded mb-2" aria-hidden="true" />
      <div className="skeleton h-4 w-72 rounded mb-6" aria-hidden="true" />
      <div aria-hidden="true"><ContentSkeleton rows={6} /></div>
      <LoadingLabel appName={appName} className="mt-4" />
    </div>
  )
}

/** A 232px left column of list rows beside the main content. Narrow-first: below
 *  `md` the rail is not drawn (an app's own rail collapses there too, and the
 *  column would leave the content a sliver), so the shape is the content alone. */
function RailShape({ appName }: { appName: string }) {
  return (
    <div className="flex-1 min-h-0 flex" data-testid="app-loading-shape-rail">
      <div className="hidden md:block w-[232px] shrink-0 border-r border-border p-3 space-y-2" aria-hidden="true" data-testid="app-loading-rail">
        <Skeleton className="h-8 w-full mb-3" />
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="flex items-center gap-2 px-1 py-1">
            <Skeleton className="h-4 w-4 shrink-0" />
            <Skeleton className="h-4 flex-1" style={{ maxWidth: `${55 + (i * 23) % 40}%` }} />
          </div>
        ))}
      </div>
      <div className="flex-1 min-w-0 px-6 pt-6 pb-8">
        <div aria-hidden="true" className="overflow-hidden"><ContentSkeleton rows={6} /></div>
        <LoadingLabel appName={appName} className="mt-4" />
      </div>
    </div>
  )
}

/** A narrow card in the middle of the page (a composer, a sign-in, a wizard). */
function CenteredShape({ appName }: { appName: string }) {
  return (
    <div className="flex-1 min-h-0 flex flex-col items-center justify-center p-8" data-testid="app-loading-shape-centered">
      <div className="w-full max-w-md rounded-lg border border-border p-6 space-y-3" aria-hidden="true" data-testid="app-loading-card">
        <Skeleton className="h-6 w-40 mx-auto" />
        <Skeleton className="h-4 w-64 max-w-full mx-auto mb-2" />
        <Skeleton className="h-24 w-full" />
        <div className="flex justify-end">
          <Skeleton className="h-8 w-20" />
        </div>
      </div>
      <LoadingLabel appName={appName} className="mt-4 text-center" />
    </div>
  )
}

/** Two panes side by side: a list and a detail view. Narrow-first: stacked below
 *  `md`, side by side from there. */
function SplitShape({ appName }: { appName: string }) {
  return (
    <div className="flex-1 min-h-0 flex flex-col md:flex-row" data-testid="app-loading-shape-split">
      <div className="flex-1 min-w-0 border-b md:border-b-0 md:border-r border-border px-6 pt-6 pb-8" data-testid="app-loading-pane">
        <div aria-hidden="true" className="overflow-hidden"><ContentSkeleton rows={6} /></div>
        <LoadingLabel appName={appName} className="mt-4" />
      </div>
      <div className="flex-1 min-w-0 px-6 pt-6 pb-8 space-y-3" aria-hidden="true" data-testid="app-loading-pane">
        <Skeleton className="h-5 w-56 max-w-full" />
        <Skeleton className="h-3 w-72 max-w-full mb-3" />
        <Skeleton className="h-48 w-full" />
      </div>
    </div>
  )
}

const SHAPES: Record<AppLoadingLayout, (p: { appName: string }) => ReactElement> = {
  page: PageShape,
  rail: RailShape,
  centered: CenteredShape,
  split: SplitShape,
}

export default function AppLoadingFallback({
  appName,
  loading,
  delayMs = APP_LOADING_DELAY_MS,
}: {
  appName: string
  /** Raw `manifest.ui.loading` value; validated here. */
  loading?: unknown
  delayMs?: number
}) {
  const visible = useDelayElapsed(delayMs)
  const reduced = useReducedMotion()
  if (!visible) return null
  const resolved = resolveAppLoadingLayout(loading)
  const Shape = SHAPES[resolved]
  return (
    <div
      role="status"
      aria-busy="true"
      className="flex-1 min-h-0 flex flex-col"
      data-testid="app-loading-fallback"
      data-layout={resolved}
      // A short fade so the placeholder arrives rather than pops. None at all
      // under reduced motion: the global rule only shortens animations, and an
      // entrance the user asked not to see should not run even briefly.
      style={reduced ? undefined : { animation: 'ft-fade 200ms ease-out both' }}
    >
      <Shape appName={appName} />
    </div>
  )
}
