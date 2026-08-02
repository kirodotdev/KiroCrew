import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Folder, RotateCw, ExternalLink, ChevronUp } from 'lucide-react'
import DetailPanel from '../../components/DetailPanel'
import { api } from '../../api/client'
import { fileIcon, colorForExt } from '../../utils/fileIcons'

/** Last path segment, trailing slashes ignored. */
function basename(p: string): string {
  return p.replace(/\/+$/, '').split('/').pop() || p
}

/**
 * Directory listing as a side-panel tab body.
 *
 * Exists because a markdown path chip pointing at a directory used to open the
 * file viewer and report "file not found" — the path was real, it just wasn't a
 * file. A directory now gets an affordance that matches what it is.
 *
 * Navigation is INTERNAL to the tab: clicking a subdirectory re-targets this
 * panel rather than spawning a tab per directory. `onPathChange` lifts the new
 * path back to the tab record so the strip label follows along. Clicking a file
 * hands off to `onFileOpen`, which opens a normal file tab.
 */
export default function FolderPanel({ path, onClose, onFileOpen, onPathChange }: {
  path: string
  onClose: () => void
  onFileOpen?: (p: string) => void
  onPathChange?: (p: string) => void
}) {
  const { t } = useTranslation()
  const [cwd, setCwd] = useState(path)

  // Re-sync when the tab is re-targeted from outside (a second chip click on a
  // different directory reuses this tab when the id matches).
  useEffect(() => { setCwd(path) }, [path])

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['browse-files', cwd],
    queryFn: () => api.browseFiles(cwd),
    retry: false,
    staleTime: 5_000,
  })

  const navigate = (next: string) => {
    setCwd(next)
    onPathChange?.(next)
  }

  const dirs = data?.dirs ?? []
  const files = data?.files ?? []
  const isEmpty = dirs.length === 0 && files.length === 0
  // `parent` comes from the backend (os.path.dirname of the resolved path).
  // Suppress the up-row at the filesystem root, where parent === path.
  const parent = data?.parent && data.parent !== data.path ? data.parent : null

  return (
    <DetailPanel
      embedded
      noPadding
      title={basename(cwd)}
      onClose={onClose}
      customHeader={
        <div className="flex items-center gap-2 h-[38px] px-3 shrink-0 border-b border-border">
          <Folder size={14} className="shrink-0 text-muted" />
          <span className="text-[12px] text-text-strong truncate" title={cwd}>{basename(cwd)}</span>
          <span className="flex-1" />
          <button
            onClick={() => refetch()}
            className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none"
            title={t('pages.chat.folderPanel.refresh')}
            aria-label={t('pages.chat.folderPanel.refresh')}
          >
            <RotateCw size={14} className={isFetching ? 'animate-spin' : undefined} />
          </button>
          <button
            onClick={() => api.revealPath(cwd)}
            className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none"
            title={t('pages.chat.folderPanel.reveal_in_finder')}
            aria-label={t('pages.chat.folderPanel.reveal_in_finder')}
          >
            <ExternalLink size={14} />
          </button>
        </div>
      }
    >
      <div className="flex-1 overflow-y-auto px-2 py-1.5">
        <div className="text-[10.5px] text-muted/80 font-mono truncate px-2 pb-1.5" title={cwd}>{cwd}</div>
        {parent && (
          <Row
            icon={<ChevronUp size={14} className="shrink-0 text-muted" />}
            label={t('pages.chat.folderPanel.parent_folder')}
            title={parent}
            onActivate={() => navigate(parent)}
          />
        )}
        {isLoading && <div className="px-2 py-2 text-[12px] text-muted">{t('pages.chat.folderPanel.loading')}</div>}
        {isError && (
          <div className="px-2 py-2 text-[12px] text-danger">
            {(error as Error)?.message || t('pages.chat.folderPanel.unable_to_list_folder')}
          </div>
        )}
        {!isLoading && !isError && isEmpty && (
          <div className="px-2 py-2 text-[12px] text-muted">{t('pages.chat.folderPanel.empty_folder')}</div>
        )}
        {dirs.map(d => (
          <Row
            key={d.path}
            icon={<Folder size={14} className="shrink-0 text-accent" />}
            label={d.name}
            title={d.path}
            onActivate={() => navigate(d.path)}
          />
        ))}
        {files.map(f => {
          const Icon = fileIcon(f.path)
          return (
            <Row
              key={f.path}
              icon={<Icon size={14} className={`shrink-0 ${colorForExt(f.path)}`} />}
              label={f.name}
              title={f.path}
              onActivate={() => onFileOpen?.(f.path)}
            />
          )
        })}
      </div>
    </DetailPanel>
  )
}

/** One listing row. Mirrors the Files tab's FileRow interaction contract:
 *  clickable, focusable, Enter/Space activates. */
function Row({ icon, label, title, onActivate }: {
  icon: React.ReactNode
  label: string
  title: string
  onActivate: () => void
}) {
  return (
    <div
      className="group flex items-center gap-2 px-2 py-1 rounded-md cursor-pointer hover:bg-bg-hover transition-colors"
      onClick={onActivate}
      title={title}
      role="button"
      tabIndex={0}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onActivate() } }}
    >
      {icon}
      <span className="min-w-0 flex-1 text-[12.5px] text-text truncate">{label}</span>
    </div>
  )
}
