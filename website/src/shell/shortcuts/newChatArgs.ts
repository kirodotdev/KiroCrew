import type { ChatFolder } from '../../types'
import { resolveFolderAgent, resolveFolderProjectDir } from '../../utils/folderAgent'

/**
 * The create the new-chat shortcut sends. When the active chat sits in a
 * folder the sidebar still lists, the new chat goes into that folder with the
 * folder's pinned agent and project directory, as the folder's own "+" does
 * (`useFolderChatCreate`). An unfiled active chat, no active chat, or a folder
 * that is gone keeps the plain New placement (the person's default folder).
 */
export function newChatShortcutArgs(
  slots: readonly { key: string; folder_id?: string }[],
  activeSlot: string | null | undefined,
  folders: readonly ChatFolder[] | undefined,
): { defaultFolder: true } | { folder_id: string; agent: string | undefined; project: string | undefined } {
  const folderId = activeSlot ? slots.find(s => s.key === activeSlot)?.folder_id : undefined
  const list = folders ? [...folders] : []
  if (!folderId || !list.some(f => f.id === folderId)) return { defaultFolder: true }
  return {
    folder_id: folderId,
    agent: resolveFolderAgent(list, folderId, ''),
    project: resolveFolderProjectDir(list, folderId),
  }
}
