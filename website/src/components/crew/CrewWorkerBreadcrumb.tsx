import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { type OpenMateSessionsState } from '../../lib/crewMode'
import { crewSubDispatcher, crewWorkerGroup, crewWorkerGroupOfRow, type CrewWorkerGroup, type CrewWorkerRowLike } from '../../lib/crewWorkers'
import { useAppDispatch, useAppSelector, useAppStore } from '../../store'
import { memberThreadTitle } from '../../lib/memberThreadTitle'
import { openCrewSession, reopenCrewWorker } from '../../pages/chat/command-center/CrewWorkerList'
import { useCrewRoot } from './useCrewRoot'
import { useCrewBack } from './useCrewBack'
import { useIsMobile } from '../../hooks/useIsMobile'
import ErrorNotice from '../ErrorNotice'
import { CrewStatusDot } from './CrewModeMarks'

/** A crumb that reads as plain text and shows it is a link only on hover or focus.
 *  On a phone the padding grows the tap target to 44px without moving the text. */
const crumbLink = 'truncate text-left cursor-pointer rounded-sm py-3 -my-3 md:py-0 md:my-0 hover:underline focus-visible:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent'

/**
 * The worker view's header: `<root title> > <worker> [status]`.
 * Shown in place of the session title when the main pane holds one of a Crew
 * Mode root's workers, so the person always sees whose worker this is. A worker
 * a sub-conductor dispatched reads `<root> > <sub-conductor> > <worker>`, and on
 * a narrow header the middle crumb gives way first. The root crumb goes back to
 * the main chat, the same action as the Crew tab's Back bar (`useCrewBack`); a
 * sub-conductor crumb opens that member like its row in the Crew tab. The
 * current member and its status stay plain text.
 *
 * `mate`: *root* is a crewmate thread and *worker* one of the sessions it
 * dispatched (its `mate_root`), opened from History, search or a link while the
 * mate sessions preview keeps it out of the person's lists. The root crumb is the
 * crewmate's name and opens its Profile card on Sessions on the Mates page,
 * where the session is listed.
 */
export function CrewWorkerBreadcrumb({ root, worker, mate = false }: { root: string; worker: CrewWorkerRowLike & { key: string; title?: string }; mate?: boolean }) {
  // Closed, not deleted: the root's tab is gone but its saved transcript stays,
  // so Back reopens it rather than switching to a session that is not listed.
  // Its name and the worker list come from saved data then, so a reload under
  // a closed root keeps both (edge review R2 Y3).
  const { workers, failed, rootClosed, rootTitle: crewRootTitle } = useCrewRoot(root)
  const back = useCrewBack(root)
  const navigate = useNavigate()
  const mateRow = useAppSelector(state => mate ? state.dashboard.slots.find(row => row.key === root) : undefined)
  // The crewmate's name, as its thread's own row reads with no roster at hand.
  const rootTitle = (mateRow && memberThreadTitle(mateRow, [])) || crewRootTitle
  // Back to the crewmate's Profile on Sessions, where this session is listed.
  const mateState: OpenMateSessionsState = { openMateSessions: true }
  const goBack = mate
    ? () => navigate(mateRow?.agent ? `/members?member=${encodeURIComponent(mateRow.agent)}` : '/members', mateRow?.agent ? { state: mateState } : undefined)
    : back.back
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const isMobile = useIsMobile()
  const [viaReopenFailed, setViaReopenFailed] = useState(false)
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
  // Open the sub-conductor the way its Crew tab row does: a closed one is
  // reopened from its saved transcript first.
  const openVia = () => {
    setViaReopenFailed(false)
    if (viaRow?.status !== 'closed') { openCrewSession(dispatch, via, !isMobile); return }
    void reopenCrewWorker(dispatch, store.getState, via, viaTitle, !isMobile).then(ok => { if (!ok) setViaReopenFailed(true) })
  }
  return (
    <div className="flex flex-col gap-1 min-w-0 w-full">
      <nav aria-label={i18nT('components.crewMode.breadcrumb_nav_label')} data-testid="crew-worker-breadcrumb" className="@container/crumb flex items-center gap-2 min-w-0 w-full pointer-events-auto">
        {/* A phone keeps a readable root prefix. A narrower split pane lets the
            names share the remaining room; tooltips retain each full name. */}
        <button type="button" onClick={goBack} aria-label={i18nT('components.crewMode.breadcrumb_back_to', { t: rootTitle })} title={rootTitle}
          className={`${crumbLink} min-w-0 @[18rem]/crumb:min-w-[5.5rem] max-w-[45%] shrink text-[13px] text-muted`} data-testid="crew-breadcrumb-root">{rootTitle}</button>
        <ChevronRight size={14} aria-hidden="true" className="text-muted shrink-0" />
        {/* A dispatcher the list does not hold yet has nothing to open: it stays a label. */}
        {via && <>
          {viaRow
            ? <button type="button" onClick={openVia} title={viaTitle}
              className={`${crumbLink} min-w-[1.5rem] max-w-[30%] shrink-[3] text-[13px] text-text`} data-testid="crew-breadcrumb-via">{viaTitle}</button>
            : <span title={viaTitle} className="truncate min-w-[1.5rem] max-w-[30%] shrink-[3] text-[13px] text-text" data-testid="crew-breadcrumb-via">{viaTitle}</span>}
          <ChevronRight size={14} aria-hidden="true" className="text-muted shrink-0" />
        </>}
        {/* Every name can shrink, even on a wide screen with a narrow chat pane.
            The status word and Back label give way to their dot and arrow first. */}
        <span className="font-semibold text-text truncate min-w-[3rem] shrink max-w-[65%]" aria-current="page" title={worker.title || undefined} data-testid="crew-breadcrumb-worker">{worker.title || i18nT('commandCenter.worker_untitled')}</span>
        <CrewStatusDot group={group} withLabel={!isMobile} className="[&>span:last-child]:sr-only @[40rem]/crumb:[&>span:last-child]:not-sr-only" />
      </nav>
      {/* A failed worker-list read is said on its own line under the row, beside
          nothing else, so no action button joins the header row. The composer
          draft is saved per chat, so the hand-off is safe. */}
      {/* A closed root is said once, by the composer's own line; its list cannot
          be read until it is reopened, which is not a failed read. */}
      {!rootClosed && failed && <ErrorNotice variant="inline" className="pointer-events-auto" message={i18nT('commandCenter.workers_error')} askAgent />}
      {((!mate && back.reopenFailed) || viaReopenFailed) && <ErrorNotice variant="inline" className="pointer-events-auto" message={i18nT('commandCenter.worker_reopen_failed')} askAgent />}
    </div>
  )
}
