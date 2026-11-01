import { useRef } from 'react'
import { ChevronRight } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { crewSubDispatcher, crewWorkerGroup, crewWorkerGroupOfRow, type CrewWorkerGroup, type CrewWorkerRowLike } from '../../lib/crewWorkers'
import { useCrewRoot } from './useCrewRoot'
import { useIsMobile } from '../../hooks/useIsMobile'
import ErrorNotice from '../ErrorNotice'
import { CrewStatusDot } from './CrewModeMarks'

/**
 * The worker view's header: `<root title> > <worker> [status]` as plain text.
 * Shown in place of the session title when the main pane holds one of a Crew
 * Mode root's workers, so the person always sees whose worker this is. A worker
 * a sub-conductor dispatched reads `<root> > <sub-conductor> > <worker>`, and on
 * a narrow header the middle crumb gives way first. The crumbs are labels with
 * no control: the one way back is the Crew tab's "Back to main chat" bar
 * (`CrewPanelBack`). The sub-conductor opens from the Crew tab like any member.
 */
export function CrewWorkerBreadcrumb({ root, worker }: { root: string; worker: CrewWorkerRowLike & { key: string; title?: string } }) {
  // Closed, not deleted: the root's tab is gone but its saved transcript stays,
  // so Back reopens it rather than switching to a session that is not listed.
  // Its name and the worker list come from saved data then, so a reload under
  // a closed root keeps both (edge review R2 Y3).
  const { workers, failed, rootClosed, rootTitle } = useCrewRoot(root)
  const isMobile = useIsMobile()
  // The server's group, as the panel and the root's row show it; the row's own
  // live flags until the worker list first answers. The last group read stays
  // while a re-read is in flight or has failed (a finished worker stays done):
  // the row's flags cannot say "done", so falling back to them on a failed poll
  // would flip a done worker to Waiting under the read-failed line.
  const listed = workers?.find(w => w.key === worker.key)
  const lastGroup = useRef<{ root: string; key: string; group: CrewWorkerGroup } | null>(null)
  if (lastGroup.current?.root !== root || lastGroup.current?.key !== worker.key) lastGroup.current = null
  if (listed) lastGroup.current = { root, key: worker.key, group: crewWorkerGroup(listed) }
  const group = lastGroup.current?.group ?? crewWorkerGroupOfRow(worker)
  // The direct dispatcher when it is a sub-conductor, from the same list.
  const via = listed ? crewSubDispatcher(listed, root) : ''
  const viaRow = via ? workers?.find(w => w.key === via) : undefined
  const viaTitle = viaRow?.title || i18nT('commandCenter.worker_untitled')
  return (
    <div className="flex flex-col gap-1 min-w-0 w-full">
      <nav aria-label={i18nT('components.crewMode.breadcrumb_label')} data-testid="crew-worker-breadcrumb" className="@container/crumb flex items-center gap-2 min-w-0 w-full pointer-events-auto">
        {/* A phone keeps a readable root prefix. A narrower split pane lets the
            names share the remaining room; tooltips retain each full name. */}
        {/* The crumbs name the sessions; the way back is the Crew tab's bar. */}
        <span title={rootTitle} className="truncate min-w-0 @[18rem]/crumb:min-w-[5.5rem] max-w-[45%] shrink text-[13px] text-muted" data-testid="crew-breadcrumb-root">{rootTitle}</span>
        <ChevronRight size={14} aria-hidden="true" className="text-muted shrink-0" />
        {via && <>
          <span title={viaTitle} className="truncate min-w-[1.5rem] max-w-[30%] shrink-[3] text-[13px] text-text" data-testid="crew-breadcrumb-via">{viaTitle}</span>
          <ChevronRight size={14} aria-hidden="true" className="text-muted shrink-0" />
        </>}
        {/* Every name can shrink, even on a wide screen with a narrow chat pane.
            The status word and Back label give way to their dot and arrow first. */}
        <span className="font-semibold text-text truncate min-w-[3rem] shrink max-w-[65%]" aria-current="page" title={worker.title || undefined} data-testid="crew-breadcrumb-worker">{worker.title || i18nT('commandCenter.worker_untitled')}</span>
        <CrewStatusDot group={group} withLabel={!isMobile} className="[&>span:last-child]:sr-only @[40rem]/crumb:[&>span:last-child]:not-sr-only" />
      </nav>
      {/* A failed worker-list read is said on its own line under the row, beside
          nothing else, so the row keeps its two controls. The composer draft is
          saved per chat, so the hand-off is safe. */}
      {/* A closed root is said once, by the composer's own line; its list cannot
          be read until it is reopened, which is not a failed read. */}
      {!rootClosed && failed && <ErrorNotice variant="inline" className="pointer-events-auto" message={i18nT('commandCenter.workers_error')} askAgent />}
    </div>
  )
}
