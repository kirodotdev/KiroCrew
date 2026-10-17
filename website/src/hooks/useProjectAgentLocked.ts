import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ChatFolder } from '../types'
import { projectRootId } from '../utils/folderAgent'

/** Whether a chat filed in `folderId` sits in a Project, whose agent is locked to
 *  the conductor server-side (`project_agent_locked`). A surface that would offer
 *  an agent picker hides it when `locked` is true.
 *
 *  Reads the shared `['chat-folders']` cache the sidebar fills, so it costs no
 *  request of its own. `error` is the folder read's failure while no folder list
 *  is on hand: the answer is then unknown rather than "not a project", and the
 *  caller shows it (the gateway still refuses an agent change in a Project). The
 *  call is guarded the way `sessionControls` guards it: a partially mocked `api`
 *  must not take the pane down over a picker decision. */
export function useProjectAgentLocked(folderId: string | null | undefined): { locked: boolean; error: Error | null } {
  const { data, error } = useQuery<ChatFolder[]>({
    queryKey: ['chat-folders'],
    queryFn: () =>
      typeof api?.chatFolders === 'function' ? api.chatFolders() : Promise.resolve([]),
  })
  const folders = Array.isArray(data) ? data : null
  return {
    locked: !!folders && !!projectRootId(folders, folderId),
    error: folders === null && folderId ? (error ?? null) : null,
  }
}
