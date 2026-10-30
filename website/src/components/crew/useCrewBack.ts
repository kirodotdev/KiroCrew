import { useCallback, useState } from 'react'
import { i18nT } from '../../i18n/t'
import { useAppDispatch, useAppStore } from '../../store'
import { useIsMobile } from '../../hooks/useIsMobile'
import { openCrewSession, reopenCrewWorker } from '../../pages/chat/command-center/CrewWorkerList'
import { useCrewRoot } from './useCrewRoot'

export interface CrewBack {
  /** Open the root in the main pane. A closed root is reopened from its saved
   *  transcript first. Desktop keeps the side panel open; a phone lands on the chat. */
  back: () => void
  /** "Back to main chat", or "Open main chat" while the root is closed. */
  label: string
  rootClosed: boolean
  rootTitle: string
  /** The last reopen of a closed root failed. */
  reopenFailed: boolean
}

/**
 * The way back from one of a Crew Mode root's workers to the root, shared by the
 * worker view's breadcrumb and the side panel's Back row so both say and do the
 * same thing. Closed, not deleted: a closed root's saved transcript stays, so Back
 * reopens it rather than switching to a session that is not listed.
 */
export function useCrewBack(root: string): CrewBack {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const isMobile = useIsMobile()
  const { rootClosed, rootTitle } = useCrewRoot(root)
  const [reopenFailed, setReopenFailed] = useState(false)
  const back = useCallback(() => {
    if (!rootClosed) {
      openCrewSession(dispatch, root, !isMobile)
      return
    }
    setReopenFailed(false)
    void reopenCrewWorker(dispatch, store.getState, root, rootTitle, !isMobile).then(ok => { if (!ok) setReopenFailed(true) })
  }, [dispatch, store, root, rootClosed, rootTitle, isMobile])
  const label = rootClosed ? i18nT('components.crewMode.reopen_main_chat') : i18nT('components.crewMode.back_to_main_chat')
  return { back, label, rootClosed, rootTitle, reopenFailed }
}
