import { useAppSelector } from '../../store'
import { i18nT } from '../../i18n/t'
import { useCrewWorkerSummary } from '../../pages/chat/command-center/CrewWorkerList'
import type { CrewWorker } from '../../types'

/** The last title each root was seen with in this tab: the name a closed root
 *  shows until its saved title is read. */
const lastRootTitle = new Map<string, string>()

export interface CrewRootView {
  /** The root's tab is closed (no live row). Closed, not deleted: its saved
   *  transcript stays and it can be reopened. */
  rootClosed: boolean
  /** The root's title: the live row's, else the saved one the worker list read,
   *  else the last one this tab saw, else the generic Crew Mode name. */
  rootTitle: string
  /** The last list a read gave, kept through a failed re-read (with the open live
   *  rows merged in), so a finished worker stays Done and a closed dispatcher stays
   *  reopenable while `failed` says the newest read did not land. */
  workers: CrewWorker[] | undefined
  failed: boolean
}

/**
 * One Crew Mode root as a worker's views see it, live or closed.
 *
 * The worker list answers for a closed root too, from saved data
 * (`root_closed`, `root_title`), so a page reloaded under a closed root keeps the
 * root's name and every worker's real state (edge review R2 Y3).
 */
export function useCrewRoot(root: string): CrewRootView {
  const rootRow = useAppSelector(s => s.dashboard.slots.find(item => item.key === root))
  const { lastWorkers, failed, savedTitle } = useCrewWorkerSummary(root)
  if (rootRow?.title) lastRootTitle.set(root, rootRow.title)
  else if (savedTitle) lastRootTitle.set(root, savedTitle)
  const rootTitle = rootRow?.title || lastRootTitle.get(root) || i18nT('components.crewMode.eyebrow')
  return { rootClosed: !rootRow, rootTitle, workers: lastWorkers, failed }
}
