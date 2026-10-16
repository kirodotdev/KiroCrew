import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Cloud, GitBranch, RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, IconButton, PanelSectionHeader } from '../../components/ui'
import { PierrePatch } from '../../pierre'
import { errorCode, remoteKey } from './RemoteFilesPanel'

interface StatusRow { code: string; path: string }

/** `git status --porcelain=v1 -z --branch` -> branch line + literal paths. */
export function parsePorcelain(output: string): { branch: string; rows: StatusRow[] } {
  let branch = ''
  const rows: StatusRow[] = []
  const records = output.split('\0')
  for (let i = 0; i < records.length; i++) {
    const record = records[i]
    if (!record) continue
    if (record.startsWith('## ')) { branch = record.slice(3); continue }
    const code = record.slice(0, 2)
    rows.push({ code, path: record.slice(3) })
    // NUL porcelain emits the destination first and the source in its own record.
    if (code.includes('R') || code.includes('C')) i++
  }
  return { branch, rows }
}

/** The Git view for a session on a REMOTE environment: the guest workdir's
 *  status, history and per-file diff, read-only, through the remote browse
 *  endpoint. The local Git panel would describe the gateway's checkout. */
export default function RemoteGitPanel({ slot, name }: { slot: string; name: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const status = useQuery({
    queryKey: [...remoteKey(slot), 'git', 'status'],
    queryFn: () => api.chatSlotEnvironmentGit(slot, 'git_status'),
    retry: false,
    staleTime: 10_000,
  })
  const log = useQuery({
    queryKey: [...remoteKey(slot), 'git', 'log'],
    queryFn: () => api.chatSlotEnvironmentGit(slot, 'git_log'),
    retry: false,
    staleTime: 30_000,
    enabled: !!status.data?.git,
  })
  const diff = useQuery({
    queryKey: [...remoteKey(slot), 'git', 'diff', selected],
    queryFn: () => api.chatSlotEnvironmentGit(slot, 'git_diff', selected ?? '.'),
    retry: false,
    enabled: !!selected && !!status.data?.git,
  })
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: [...remoteKey(slot), 'git'] }) }
  const failed = (e: unknown) =>
    errorCode(e) === 'workspace_stopped' ? t('sessionEnvironments.remoteFiles.stopped') : t('sessionEnvironments.remoteFiles.error')

  const header = (
    <div className="flex items-center gap-2 px-3 h-9 border-b border-border shrink-0">
      <Cloud size={14} className="shrink-0 text-accent" aria-hidden />
      <span className="text-[12.5px] text-text truncate flex-1">{t('sessionEnvironments.remoteGit.title', { name })}</span>
      <IconButton aria-label={t('sessionEnvironments.remoteFiles.refresh')} title={t('sessionEnvironments.remoteFiles.refresh')} onClick={refresh}>
        <RefreshCw className="lucide-inline" />
      </IconButton>
    </div>
  )
  if (status.isError || status.data?.error_text) {
    return <div className="h-full flex flex-col">{header}<div className="p-3"><ErrorNotice message={status.data?.error_text || failed(status.error)} askAgent /></div></div>
  }
  if (!status.data) {
    return <div className="h-full flex flex-col">{header}<div className="p-3 text-[12.5px] text-muted" aria-busy="true">{t('sessionEnvironments.remoteFiles.loading')}</div></div>
  }
  if (!status.data.git) {
    return (
      <div className="h-full flex flex-col">
        {header}
        <div className="p-6 text-center text-[13px] text-muted">{t('sessionEnvironments.remoteGit.notRepo')}</div>
      </div>
    )
  }
  const { branch, rows } = parsePorcelain(status.data.output ?? '')
  const commits = (log.data?.output ?? '').split('\n').filter(Boolean).map((line) => {
    const [hash, author, when, ...subject] = line.split('\t')
    return { hash, author, when, subject: subject.join('\t') }
  })
  return (
    <div className="h-full flex flex-col min-h-0" data-testid="remote-git-panel">
      {header}
      <div className="flex-1 min-h-0 overflow-auto px-3 py-2 flex flex-col gap-3">
        <div className="flex items-center gap-1.5 text-[12.5px] text-text">
          <GitBranch className="lucide-inline shrink-0 text-muted" aria-hidden />
          <span className="font-mono truncate">{branch}</span>
        </div>
        <div>
          <PanelSectionHeader label={t('sessionEnvironments.remoteGit.changes')} count={rows.length} />
          {rows.length === 0 ? (
            <div className="py-2 text-[12.5px] text-muted">{t('sessionEnvironments.remoteGit.clean')}</div>
          ) : (
            <div className="mt-1 flex flex-col">
              {rows.map((row) => (
                <Btn
                  key={row.path}
                  type="button"
                  aria-pressed={selected === row.path}
                  onClick={() => setSelected(row.path === selected ? null : row.path)}
                  className={`justify-start gap-2 h-7 rounded-md border-none bg-transparent text-[12.5px] font-normal hover:bg-bg-hover ${selected === row.path ? 'bg-bg-hover' : ''}`}
                >
                  <span className="font-mono text-[11.5px] text-muted w-[2ch] shrink-0">{row.code.trim() || '·'}</span>
                  <span className="font-mono truncate">{row.path}</span>
                </Btn>
              ))}
            </div>
          )}
        </div>
        {selected && (
          <div className="min-h-0">
            {diff.isError || diff.data?.error_text ? (
              <ErrorNotice variant="inline" message={diff.data?.error_text || failed(diff.error)} askAgent />
            ) : diff.data ? (
              diff.data.output ? (
                <PierrePatch patch={diff.data.output} />
              ) : (
                <div className="py-2 text-[12.5px] text-muted">{t('sessionEnvironments.remoteGit.noDiff')}</div>
              )
            ) : (
              <div className="py-2 text-[12.5px] text-muted" aria-busy="true">{t('sessionEnvironments.remoteFiles.loading')}</div>
            )}
          </div>
        )}
        <div>
          <PanelSectionHeader label={t('sessionEnvironments.remoteGit.history')} count={commits.length} />
          {(log.isError || log.data?.error_text) && <ErrorNotice variant="inline" message={log.data?.error_text || failed(log.error)} askAgent />}
          <div className="mt-1 flex flex-col gap-1">
            {commits.map((c) => (
              <div key={c.hash} className="flex items-baseline gap-2 text-[12.5px] min-w-0">
                <span className="font-mono text-[11.5px] text-muted shrink-0">{c.hash}</span>
                <span className="truncate flex-1 text-text" title={c.subject}>{c.subject}</span>
                <span className="text-[11.5px] text-muted shrink-0">{c.when}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
