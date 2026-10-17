import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock, FolderX } from 'lucide-react'
import { api } from '../../api/client'
import { invalidateFoldersWhenIdle } from '../../api/chatFoldersWrite'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import type { ChatFolder } from '../../types'

/** "a / b / c" for a folder, walking its parent chain. Guarded against a cycle. */
export function folderPath(folders: ChatFolder[], id: string): string {
  const byId = new Map(folders.map(f => [f.id, f]))
  const names: string[] = []
  const seen = new Set<string>()
  let cur = byId.get(id)
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id)
    names.unshift(cur.name)
    cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
  }
  return names.join(' / ')
}

/**
 * The header ⋮ → "Clean up empty folders" panel: a dry-run preview of every
 * folder with no live session and no settings, then one Delete for all of
 * them. Delete sends back the previewed ids and the server deletes only those
 * that are still empty, re-checked inside the folder-store lock: a session
 * filed after the preview keeps its folder, and a folder that emptied after
 * the preview (so was never shown) is not deleted.
 */
export default function FolderCleanupPanel({ folders, onClose }: { folders: ChatFolder[]; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [includeTopLevel, setIncludeTopLevel] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const preview = useQuery({
    queryKey: ['folder-cleanup-preview', includeTopLevel],
    queryFn: () => api.cleanupChatFolders({ dryRun: true, includeTopLevel }),
    gcTime: 0,
  })
  const archived = preview.data?.archived ?? {}
  const previewIds = preview.data?.ids
  const rows = useMemo(() => (previewIds ?? []).map(id => ({ id, path: folderPath(folders, id) })), [previewIds, folders])
  const cleanup = useMutation({
    mutationFn: () => api.cleanupChatFolders({ includeTopLevel, ids: previewIds ?? [] }),
    onSuccess: (res) => {
      // Through the idle gate, not a direct invalidate: this refetch of the
      // folder tree would otherwise land inside a still-pending optimistic
      // folder write in the sidebar and snap it back (see CHAT_FOLDERS_WRITE_KEY).
      invalidateFoldersWhenIdle(queryClient)
      queryClient.invalidateQueries({ queryKey: ['folder-cleanup-preview'] })
      // The server keeps any previewed folder that gained a session or a new
      // subfolder after the preview. Closing then would look as if they went
      // too; stay open on the fresh preview and say why some stayed.
      if ((res.deleted?.length ?? 0) < (previewIds?.length ?? 0)) {
        setNotice(i18nT('pages.chatSidebar.some_folders_kept'))
        return
      }
      onClose()
    },
    onError: (e) => setError(e instanceof Error ? e.message : i18nT('components.errorBoundary.something_went_wrong')),
  })
  return (
    <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise" data-testid="folder-cleanup-panel">
      <div className="font-medium text-text-strong mb-2"><FolderX size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.clean_up_empty_folders')}</div>
      <div className="text-muted text-[12px] mb-2">{i18nT('pages.chatSidebar.empty_folders_explainer')}</div>
      <label className="flex items-center gap-2 text-[12px] text-muted mb-2 cursor-pointer">
        <input type="checkbox" checked={includeTopLevel} onChange={e => setIncludeTopLevel(e.target.checked)} aria-label={i18nT('pages.chatSidebar.include_top_level_folders')} data-testid="folder-cleanup-top-level" />
        {i18nT('pages.chatSidebar.include_top_level_folders')}
      </label>
      <div className="text-[12px] text-muted mb-3">
        {preview.isLoading
          ? i18nT('pages.chatSidebar.checking')
          : preview.isError
            ? (
              <span className="inline-flex items-center gap-2 flex-wrap">
                <ErrorNotice message={i18nT('pages.chatSidebar.failed_to_load_preview')} variant="inline" askAgent testId="folder-cleanup-preview-error" />
                <Btn className="text-[12px] px-2 py-0.5" onClick={() => preview.refetch()}>{i18nT('pages.chatSidebar.retry')}</Btn>
              </span>
            )
            : rows.length === 0
              ? i18nT('pages.chatSidebar.no_empty_folders')
              : (
                <div className="max-h-40 overflow-y-auto rounded-md border border-border bg-bg-elevated p-1.5" data-testid="folder-cleanup-list">
                  {rows.map(r => (
                    <div key={r.id} className="flex items-center gap-1 text-[12px] text-muted py-0.5 px-1">
                      <span className="truncate" title={r.path}>{r.path}</span>
                      {archived[r.id] ? (
                        <span
                          className="ml-auto shrink-0 inline-flex items-center gap-0.5 text-[11px] opacity-60"
                          title={i18nT('pages.chatSidebar.archived_sessions_kept', { count: archived[r.id] })}
                          aria-label={i18nT('pages.chatSidebar.archived_sessions_kept', { count: archived[r.id] })}
                          data-testid="folder-cleanup-archived"
                        ><Clock size={11} aria-hidden="true" />{i18nT('pages.chatSidebar.archived_count', { count: archived[r.id] })}</span>
                      ) : null}
                    </div>
                  ))}
                </div>
              )}
      </div>
      {notice && <div className="text-[12px] text-muted mb-2" role="status" data-testid="folder-cleanup-notice">{notice}</div>}
      <ErrorNotice message={error} askAgent className="mb-2" testId="folder-cleanup-error" />
      <div className="flex flex-wrap items-center gap-2 justify-end">
        <Btn className="text-[12px] px-3 py-1" onClick={onClose}>{i18nT('pages.chatSidebar.cancel')}</Btn>
        {rows.length > 0 && (
          <Btn
            danger
            className="text-[12px] px-2 py-1 whitespace-nowrap"
            data-testid="folder-cleanup-confirm"
            disabled={cleanup.isPending || preview.isFetching}
            onClick={() => { setError(''); setNotice(''); cleanup.mutate() }}
          >{cleanup.isPending ? i18nT('pages.chatSidebar.deleting_folders') : i18nT('pages.chatSidebar.delete_empty_folders', { count: rows.length })}</Btn>
        )}
      </div>
    </div>
  )
}
