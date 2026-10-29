import { ChevronRight } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { useAppDispatch } from '../../store'
import { openActivityPanel } from '../../store/chatSlice'
import { openPanelView } from '../../hooks/usePanelTabs'
import { CREW_GROUP_LABEL_KEY, CREW_GROUP_TONE, groupCrewWorkers } from '../../lib/crewWorkers'
import { useCrewWorkerSummary } from '../../pages/chat/command-center/CrewWorkerList'
import ErrorNotice from '../ErrorNotice'

/**
 * A conductor's workers at a glance, above its composer while the side panel is
 * closed: one chip per state with its count, approvals first in their warning
 * tone. An approval is decided on `CrewApprovalCards` beside it, so the strip
 * only counts it. Pressing it opens the side panel on the Workers tab, which
 * its end names on screen.
 *
 * It reads what the panel and the session row read (`useCrewWorkerSummary`: the
 * shared worker-list query merged with the live slot rows the socket keeps
 * current), so it adds no read and no state of its own. Nothing is drawn while
 * the conductor has no workers. A failed re-read is said under the counts, which
 * are then the last ones read, not today's.
 *
 * `beforeOpen` runs first on a press: the chat's find pane hides the side panel
 * while it is open, so the caller closes it there.
 */
export function CrewStatusStrip({ root, beforeOpen }: { root: string; beforeOpen?: () => void }) {
  const dispatch = useAppDispatch()
  const { lastWorkers, failed } = useCrewWorkerSummary(root)
  const groups = groupCrewWorkers(lastWorkers ?? [])
  // Nothing to show and nothing failed: the conductor has no workers yet.
  if (groups.length === 0 && !failed) return null
  const open = () => {
    beforeOpen?.()
    openPanelView(root, 'workers')
    dispatch(openActivityPanel())
  }
  return (
    // Its own layer in the composer's status stack, the same as the task and
    // workflow bars beside it (ChatPage.statusStackLayering pins it).
    <div className="px-4 mx-auto w-full relative z-[2]" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      {groups.length > 0 && <button
        type="button"
        onClick={open}
        title={i18nT('components.crewMode.status_strip_open')}
        data-testid="crew-status-strip"
        className="mb-1.5 flex w-full min-h-8 flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border bg-bg-elevated/60 px-2.5 py-1 text-left text-[12px] cursor-pointer hover:bg-bg-hover transition-colors motion-reduce:transition-none focus-ring-accent [@media(pointer:coarse)]:min-h-11"
      >
        {/* The button's name: what it is and what pressing it does, then each count. */}
        <span className="sr-only">{i18nT('components.crewMode.status_strip_open')}</span>
        {groups.map(({ group, workers }) => (
          <span key={group} data-testid="crew-status-chip" data-group={group} className={`inline-flex items-center gap-1 whitespace-nowrap ${CREW_GROUP_TONE[group]}`}>
            <span aria-hidden="true" className={`w-2 h-2 rounded-full ${group === 'waiting' ? 'border border-current' : 'bg-current'}`} />
            <span>{i18nT(CREW_GROUP_LABEL_KEY[group])}</span>
            <span data-testid="crew-status-count" className="tabular-nums">{workers.length}</span>
          </span>
        ))}
        {/* What a press opens, said on screen: the side panel's Crew tab. */}
        <span aria-hidden="true" data-testid="crew-status-open" className="ml-auto inline-flex items-center gap-0.5 whitespace-nowrap text-muted">
          {i18nT('commandCenter.workers')}
          <ChevronRight size={14} />
        </span>
      </button>}
      {/* The composer's draft is saved per chat, so the hand-off is safe. */}
      {failed && <ErrorNotice variant="inline" className="mb-1.5" message={i18nT('commandCenter.workers_error')} askAgent />}
    </div>
  )
}
