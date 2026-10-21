import { ChevronLeft, ChevronRight } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { useAppDispatch, useAppSelector } from '../../store'
import { crewWorkerGroup, crewWorkerGroupOfRow, type CrewWorkerRowLike } from '../../lib/crewWorkers'
import { openCrewSession, useCrewWorkerSummary } from '../../pages/chat/command-center/CrewWorkerList'
import { useIsMobile } from '../../hooks/useIsMobile'
import { CrewGhostBadge, CrewStatusDot } from './CrewModeMarks'

/**
 * The worker view's header: `[ghost] <root title> > <worker> [status]` and a
 * "Back to conductor" button. Shown in place of the session title when the main
 * pane holds one of a Crew Mode root's workers, so the person always sees whose
 * worker this is and has one click back. The root title is a button too.
 */
export function CrewWorkerBreadcrumb({ root, worker }: { root: string; worker: CrewWorkerRowLike & { key: string; title?: string } }) {
  const dispatch = useAppDispatch()
  const rootTitle = useAppSelector(s => s.dashboard.slots.find(item => item.key === root)?.title) || i18nT('components.crewMode.eyebrow')
  const isMobile = useIsMobile()
  // The server's group, as the panel and the root's row show it; the row's own
  // flags only until the worker list answers.
  const listed = useCrewWorkerSummary(root)?.find(w => w.key === worker.key)
  const group = listed ? crewWorkerGroup(listed) : crewWorkerGroupOfRow(worker)
  const back = () => openCrewSession(dispatch, root, !isMobile)
  return (
    <nav aria-label={i18nT('components.crewMode.breadcrumb_label')} data-testid="crew-worker-breadcrumb" className="@container/crumb flex items-center gap-2 min-w-0 w-full pointer-events-auto">
      {/* The root keeps room for its first word or two (about seven characters)
          on a phone, so it never collapses to one letter; the full name is the
          tooltip. Below that the worker's name gives way, not the root's. */}
      <button type="button" onClick={back} title={rootTitle} className="flex items-center gap-1.5 min-w-[5.5rem] max-w-[45%] shrink text-[13px] crew-grad-text font-medium cursor-pointer bg-transparent border-none p-0 hover:underline" data-testid="crew-breadcrumb-root">
        <CrewGhostBadge size={18} />
        <span className="truncate">{rootTitle}</span>
      </button>
      <ChevronRight size={14} aria-hidden="true" className="text-muted shrink-0" />
      {/* The worker's name keeps its room on a wide header; on a narrow one it
          shares the row with the root's minimum and truncates, with its full name
          as the tooltip. The status is a dot there (its word stays for readers). */}
      <span className="font-semibold text-text truncate min-w-[3rem] shrink @[30rem]/crumb:shrink-0 @[30rem]/crumb:max-w-[65%]" aria-current="page" title={worker.title || undefined} data-testid="crew-breadcrumb-worker">{worker.title || i18nT('commandCenter.worker_untitled')}</span>
      <CrewStatusDot group={group} withLabel={!isMobile} />
      <button type="button" onClick={back} data-testid="crew-back-to-conductor" aria-label={i18nT('components.crewMode.back_to_conductor')} title={i18nT('components.crewMode.back_to_conductor')}
        className="ml-auto shrink-0 inline-flex items-center justify-center gap-1 h-8 min-h-8 px-2.5 rounded-lg border border-border bg-bg-elevated text-[12px] text-text hover:bg-bg-hover cursor-pointer max-md:min-h-11 max-md:min-w-11 [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11">
        <ChevronLeft size={14} aria-hidden="true" />
        <span className="hidden @[30rem]/crumb:inline">{i18nT('components.crewMode.back_to_conductor')}</span>
      </button>
    </nav>
  )
}
