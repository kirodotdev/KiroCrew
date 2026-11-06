import type { ChatFolder } from '../../types'
import { resolveFolderAgent, resolveFolderProjectDir } from '../../utils/folderAgent'
import { isMateManaged } from '../../utils/mateFolders'

/**
 * The create the new-chat shortcut sends. When the active chat sits in a
 * folder the sidebar still lists, the new chat goes into that folder with the
 * folder's pinned agent and project directory, as the folder's own "+" does
 * (`useFolderChatCreate`). An unfiled active chat, no active chat, or a folder
 * that is gone keeps the plain New placement (the person's default folder),
 * and so does a folder a mate manages, which takes only that mate's chats.
 * Either way it is the person's own New chat, so it asks for their Ultra Mode
 * default (`crewModeDefault`).
 */
export function newChatShortcutArgs(
  slots: readonly { key: string; folder_id?: string }[],
  activeSlot: string | null | undefined,
  folders: readonly ChatFolder[] | undefined,
): { defaultFolder: true; crewModeDefault: true } | { folder_id: string; agent: string | undefined; project: string | undefined; crewModeDefault: true } {
  const folderId = activeSlot ? slots.find(s => s.key === activeSlot)?.folder_id : undefined
  const list = folders ? [...folders] : []
  const folder = folderId ? list.find(f => f.id === folderId) : undefined
  if (!folderId || !folder || isMateManaged(folder, list)) return { defaultFolder: true, crewModeDefault: true }
  return {
    crewModeDefault: true,
    folder_id: folderId,
    agent: resolveFolderAgent(list, folderId, ''),
    project: resolveFolderProjectDir(list, folderId),
  }
}
