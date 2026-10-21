/**
 * Settings ▸ Chat ▸ Sessions ▸ Default Sidebar Folder for New Chats.
 *
 * Lists the person's own folders by path. A saved folder that has since been
 * deleted stays visible as its own option, so the control shows what is stored
 * and picking None clears it; new chats are unfiled meanwhile either way.
 */
import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { fetchDashboardConfig } from '../../api/dashboardConfigQuery'
import { SettingsSelect } from '../../components/settings'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useFolderSortMode } from '../../hooks/useFolderSortMode'
import { i18nT } from '../../i18n/t'
import type { ChatFolder } from '../../types'
import { orderFoldersWithPaths } from '../../utils/folderTree'
import type { DashboardConfig } from '../chat/ChatSettings'

/** Whether *folder* may be the default: the person's own, never an app's or a member's. */
const folderCanBeDefault = (folder: ChatFolder): boolean => !folder.owner_app

export function DefaultSessionFolderSetting({ disabled }: { disabled?: boolean }) {
  const { data: folders = [], isSuccess: foldersLoaded, isError: foldersFailed, refetch: refetchFolders, isFetching: foldersFetching } = useQuery<ChatFolder[]>({
    queryKey: ['chat-folders'],
    queryFn: () => api.chatFolders(),
  })
  // The value is a folder ID (`dashboard.default_session_folder`), which
  // survives a rename or a move. Applying it is the server's job: `createSlot`
  // only asks for the default, and the create route decides whether one is set
  // and still exists.
  const qc = useQueryClient()
  const config = useQuery<DashboardConfig>({ queryKey: ['dashboardConfig'], queryFn: fetchDashboardConfig })
  const save = useMutation({
    mutationFn: (id: string) => api.updateDashboardConfig({ default_session_folder: id }),
    onSuccess: (_data, id) => {
      qc.setQueryData<DashboardConfig>(['dashboardConfig'], cached =>
        cached ? { ...cached, default_session_folder: id } : cached)
    },
    onSettled: () => { void qc.invalidateQueries({ queryKey: ['dashboardConfig'] }) },
  })
  // A pick still saving is what the person last chose, so the control shows
  // it rather than flicking back to the old value until the save lands.
  const folderId = save.isPending ? save.variables ?? '' : config.data?.default_session_folder ?? ''
  const loaded = config.isSuccess
  const saving = save.isPending
  const error = save.error
  const setFolderId = (id: string) => save.mutate(id)
  // The sidebar's folder order (`dashboard.folder_sort`), as every folder
  // picker outside the sidebar draws it.
  const { mode: folderSortMode } = useFolderSortMode()
  const { options, labels } = useMemo(() => {
    const ordered = orderFoldersWithPaths(folders.filter(folderCanBeDefault), folderSortMode)
    const ids = ['', ...ordered.map(o => o.folder.id)]
    // Two folders can share a path (same name, same parent). Number the later
    // ones so the list never offers two options that read the same.
    const seen = new Map<string, number>()
    const paths = ordered.map(o => {
      const n = (seen.get(o.path) ?? 0) + 1
      seen.set(o.path, n)
      return n === 1 ? o.path : `${o.path} (${n})`
    })
    const names = [i18nT('pages.settings.chatPanel.default_session_folder_none'), ...paths]
    // Only once the folder list has loaded: before that every saved ID would
    // read as deleted for a moment.
    if (foldersLoaded && folderId && !ids.includes(folderId)) {
      ids.push(folderId)
      names.push(i18nT('pages.settings.chatPanel.default_session_folder_deleted'))
    }
    return { options: ids, labels: names }
  }, [folders, foldersLoaded, folderId, folderSortMode])
  const stale = foldersLoaded && !!folderId && !folders.some(f => f.id === folderId && folderCanBeDefault(f))
  return (
    <>
      <SettingsSelect
        label={i18nT('pages.settings.chatPanel.default_session_folder')}
        description={i18nT('pages.settings.chatPanel.default_session_folder_desc')}
        hint={i18nT('pages.settings.chatPanel.default_session_folder_hint')}
        value={folderId}
        options={options}
        optionLabels={labels}
        onChange={setFolderId}
        disabled={disabled || !loaded || saving}
        configKey="dashboard.default_session_folder"
      />
      {/* On the row, not in the info tip: it explains why new chats are not
          going where the control says. */}
      {stale && (
        <p role="status" className="text-[12px] text-text">
          {i18nT('pages.settings.chatPanel.default_session_folder_stale_hint')}
        </p>
      )}
      {/* Without the folder list the picker would offer only None and read as
          "you have no folders". The hand-off loses nothing: nothing here is
          unsaved while the list is missing. */}
      {foldersFailed && (
        <div className="flex flex-wrap items-center gap-2">
          <ErrorNotice
            variant="inline"
            askAgent
            message={i18nT('pages.settings.chatPanel.default_session_folder_load_failed')}
          />
          <Btn onClick={() => { void refetchFolders() }} disabled={foldersFetching}>
            {i18nT('pages.settings.chatPanel.retry')}
          </Btn>
        </div>
      )}
      {/* The hand-off loses nothing: a failed pick has nothing left unsaved,
          because the control already shows the stored value again. */}
      <ErrorNotice
        variant="inline"
        askAgent
        message={error ? i18nT('pages.settings.chatPanel.default_session_folder_save_failed') : null}
      />
    </>
  )
}
