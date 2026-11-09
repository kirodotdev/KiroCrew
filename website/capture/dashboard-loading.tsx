/**
 * Isolated capture entry for the Dashboard tab's loading state (#24610).
 *
 * Shows the loading panel at the size the Dashboard tab gives it, with the
 * shipped stylesheet and theme tokens imported so a screenshot photographs the
 * real colours. The `before` variant reproduces the three placeholders that used
 * to split the one wait between them (the Suspense sentence, the two thin pulsing
 * bars, and the muted "Drawing…" sentence); the `after` variant mounts the one
 * shared `CrewDashboardLoading` treatment all three now render.
 *
 * Scenes: ?variant=before|after
 * Theme:  &theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import CrewDashboardLoading from '../src/pages/members/CrewDashboardLoading'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const variant = params.get('variant') === 'before' ? 'before' : 'after'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

// The Dashboard tab is a tall panel beside the chat; the loading state fills it.
function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ width: 420, height: 560, display: 'flex' }} className="border border-border bg-bg text-text">
      <div className="h-full min-h-0 flex-1 flex flex-col">{children}</div>
    </div>
  )
}

// The three placeholders as they were before this change, drawn in the order the
// three serial waits hit them. The copy is inlined because this change removed
// the `webview_rendering` catalog key the Suspense sentence used; this harness
// only illustrates the removed state.
function Before() {
  return (
    <>
      <p role="status" className="text-sm text-muted p-4 pb-0">Rendering the published view…</p>
      <div className="p-4 space-y-1.5">
        <div className="h-3 rounded bg-bg-hover animate-pulse" />
        <div className="h-3 w-2/3 rounded bg-bg-hover animate-pulse" />
      </div>
      <div className="p-4 text-[11px] text-muted">Drawing the dashboard…</div>
    </>
  )
}

createRoot(document.getElementById('root')!).render(
  <Frame>{variant === 'before' ? <Before /> : <CrewDashboardLoading />}</Frame>,
)
