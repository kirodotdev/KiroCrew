import { useQuery } from '@tanstack/react-query'
import { X, MessageSquare, Folder as FolderIcon } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'
import { useAppSelector } from '../../store'
import type { ChatFolder } from '../../types'
import { i18nT } from '../../i18n/t'
import type { ReactNode } from 'react'

/** The query parameters that narrow the library to a chat's involvement. */
export type ArtifactChatScopeParam = 'touched_by' | 'chat_folder'

/**
 * One dismissible chip per active chat scope on the /artifacts page.
 *
 * `touched_by` names a chat slot and `chat_folder` names a sidebar folder; the
 * server resolves both, so this component only labels them. A chat is labelled
 * by its live title and a folder by its sidebar name, falling back to a generic
 * label when the chat or folder is not known to this tab. A folder list that
 * fails to load is reported through `ErrorNotice` above the chips, since the
 * generic label alone would read as an unknown folder. Dismissing a chip hands
 * its parameter to `onClear`, which drops it from the URL.
 */
export default function ArtifactScopeChips({ touchedBy, chatFolder, onClear, askAgent }: {
  touchedBy: string
  chatFolder: string
  onClear: (param: ArtifactChatScopeParam) => void
  /** Whether the failure notice may offer the agent hand-off (the page's own decision). */
  askAgent?: boolean
}) {
  const chatTitle = useAppSelector(
    s => (touchedBy ? s.dashboard.slots.find(x => x.key === touchedBy)?.title || '' : ''),
  )
  // The sidebar's own folder cache; fetched only when a folder scope needs a name.
  const { data: chatFolders, error: chatFoldersError } = useQuery<ChatFolder[]>({
    queryKey: ['chat-folders'],
    queryFn: () => api.chatFolders(),
    enabled: !!chatFolder,
    staleTime: 60_000,
  })
  const folderName = chatFolder && Array.isArray(chatFolders)
    ? chatFolders.find(f => f.id === chatFolder)?.name || ''
    : ''
  if (!touchedBy && !chatFolder) return null
  return (
    <>
      {chatFolder && chatFoldersError && (
        <ErrorNotice
          className="mb-3"
          title={i18nT('pages.artifactsPage.scope_chat_folder_load_failed')}
          message={chatFoldersError instanceof Error ? chatFoldersError.message : String(chatFoldersError)}
          askAgent={askAgent}
        />
      )}
      <div
        className="flex flex-wrap items-center gap-2 mb-3"
        role="group"
        aria-label={i18nT('pages.artifactsPage.scope_group')}
        data-testid="artifact-scope-chips"
      >
        {touchedBy && (
          <ScopeChip
            icon={<MessageSquare size={12} aria-hidden />}
            label={chatTitle
              ? i18nT('pages.artifactsPage.scope_chat', { name: chatTitle })
              : i18nT('pages.artifactsPage.scope_chat_unnamed')}
            onClear={() => onClear('touched_by')}
          />
        )}
        {chatFolder && (
          <ScopeChip
            icon={<FolderIcon size={12} aria-hidden />}
            label={folderName
              ? i18nT('pages.artifactsPage.scope_chat_folder', { name: folderName })
              : i18nT('pages.artifactsPage.scope_chat_folder_unnamed')}
            onClear={() => onClear('chat_folder')}
          />
        )}
      </div>
    </>
  )
}

function ScopeChip({ icon, label, onClear }: { icon: ReactNode; label: string; onClear: () => void }) {
  return (
    <span className="inline-flex items-center gap-1.5 max-w-full rounded-full border border-border bg-bg-elevated pl-2.5 pr-1 py-0.5 text-[12px] text-text">
      {icon}
      <span className="truncate">{label}</span>
      <Btn
        type="button"
        onClick={onClear}
        aria-label={i18nT('pages.artifactsPage.scope_clear', { name: label })}
        title={i18nT('pages.artifactsPage.scope_clear', { name: label })}
        className="justify-center rounded-full p-0.5 border-transparent bg-transparent text-muted hover:text-text hover:bg-bg-hover hover:border-transparent"
      >
        <X size={12} aria-hidden />
      </Btn>
    </span>
  )
}
