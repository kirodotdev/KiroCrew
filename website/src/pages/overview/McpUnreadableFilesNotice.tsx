/**
 * The line the MCP servers list shows for each MCP config file it skipped
 * because the file is saved as UTF-16 or UTF-32 (Windows PowerShell 5.1's `>`
 * writes UTF-16). Such a file adds no rows, so without this line its servers
 * are just missing. The reason is the gateway's fixed repair text, the same
 * one the chat's session-start notice gives.
 */
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import type { McpUnreadableFile } from '../../types'
import { isPermanentRefusal } from './McpShareRefusedNotice'

export default function McpUnreadableFilesNotice({
  listReadAt,
  className = '',
  stagedChanges = false,
}: {
  /**
   * When the servers list was last read (its query's `dataUpdatedAt`, 0 before
   * the first read). Keyed on it, so a re-read of the list re-reads this too and
   * a file re-saved as UTF-8 drops its line when its servers appear.
   */
  listReadAt: number
  className?: string
  /** The host page holds staged edits that are not applied yet, so the chat hand-off is off. */
  stagedChanges?: boolean
}) {
  const { data, isError, error } = useQuery<{ files: McpUnreadableFile[] }>({
    queryKey: ['mcp-unreadable-files', listReadAt],
    queryFn: () => api.mcpUnreadableFiles(),
    enabled: listReadAt > 0,
    placeholderData: keepPreviousData,
  })
  // A gateway without the route (404) has nothing to report, as before it existed.
  if (isError && !isPermanentRefusal(error)) {
    return (
      <ErrorNotice
        className={className}
        message={i18nT('pages.overview.mcpTab.unreadable_files_failed')}
        askAgent={!stagedChanges}
        testId="mcp-unreadable-files-error"
      />
    )
  }
  return (
    <>
      {(data?.files ?? []).map(entry => (
        <ErrorNotice
          key={entry.file}
          className={className}
          message={i18nT('pages.overview.mcpTab.unreadable_file', { file: entry.file, reason: entry.reason })}
          askAgent={!stagedChanges}
          testId="mcp-unreadable-file"
        />
      ))}
    </>
  )
}
