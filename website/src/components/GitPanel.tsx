import { useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { FolderGit2, GitBranch, RefreshCw } from 'lucide-react'
import { api } from '../api/client'
import DetailPanel from './DetailPanel'
import { i18nT } from '../i18n/t'
import { fmtUnit } from '../i18n/format'

/** Relative time label from ISO date string. */
function relativeTime(iso: string): string {
  const now = Date.now()
  const then = new Date(iso).getTime()
  const secs = Math.round((now - then) / 1000)
  if (secs < 60) return i18nT('components.gitPanel.just_now')
  const mins = Math.round(secs / 60)
  if (mins < 60) return fmtUnit(mins, 'minute')
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return fmtUnit(hrs, 'hour')
  const days = Math.round(hrs / 24)
  if (days < 30) return fmtUnit(days, 'day')
  const months = Math.round(days / 30)
  return fmtUnit(months, 'month')
}

/** Status letter color class. */
function statusColor(s: string): string {
  switch (s) {
    case 'M': return 'text-warn'
    case 'A': return 'text-ok'
    case 'D': return 'text-danger'
    case '?': return 'text-info'
    default: return 'text-muted'
  }
}

/** Dimmed directory prefix, highlighted filename. */
function FilePath({ path }: { path: string }) {
  const lastSlash = path.lastIndexOf('/')
  if (lastSlash < 0) return <span className="font-mono text-[12px]">{path}</span>
  return (
    <span className="font-mono text-[12px]">
      <span className="text-muted">{path.slice(0, lastSlash + 1)}</span>
      {path.slice(lastSlash + 1)}
    </span>
  )
}

interface GitPanelProps {
  projectDir: string
  onFileOpen?: (path: string) => void
  onClose: () => void
}

export default function GitPanel({ projectDir, onFileOpen, onClose }: GitPanelProps) {
  const prevBranch = useRef<string | undefined>(undefined)
  const forceFresh = useRef(false)

  const { data: status, refetch: refetchStatus, isLoading: statusLoading } = useQuery({
    queryKey: ['git-status', projectDir],
    queryFn: () => {
      const fresh = forceFresh.current
      forceFresh.current = false
      return api.projectGitStatus(projectDir, fresh)
    },
    enabled: !!projectDir,
    refetchInterval: 5000,
    refetchOnWindowFocus: true,
    retry: 1,
  })

  const { data: log, refetch: refetchLog } = useQuery({
    queryKey: ['git-log', projectDir],
    queryFn: () => api.projectGitLog(projectDir),
    enabled: !!projectDir,
    staleTime: 30_000,
    retry: 1,
  })

  // Refetch log when the branch changes or the branch moves ahead of its
  // upstream (a new local commit) — otherwise the Commits list goes stale
  // until the next manual refresh.
  useEffect(() => {
    const marker = status?.branch ? `${status.branch}@${status.ahead ?? 0}` : undefined
    if (marker && prevBranch.current && marker !== prevBranch.current) {
      refetchLog()
    }
    prevBranch.current = marker
  }, [status?.branch, status?.ahead, refetchLog])

  const fileCount = status?.files?.length ?? 0
  // A single-repo answer is one unnamed group (no header).
  const groups = useMemo(
    () =>
      status?.repos?.length
        ? status.repos.map(r => ({ root: r.root, name: r.name, branch: r.branch, refused: r.refused, truncated: r.truncated, files: r.files }))
        : [{ root: status?.repoRoot ?? '', name: '', branch: undefined as string | undefined, refused: status?.refused, truncated: undefined as boolean | undefined, files: status?.files ?? [] }],
    [status],
  )
  const isClean = fileCount === 0

  return (
    <DetailPanel
      embedded
      title={i18nT('components.gitPanel.title')}
      onClose={onClose}
      noPadding
      customHeader={
        <div className="flex items-center gap-2 h-[38px] px-3 shrink-0 border-b border-border">
          {/* Branch name */}
          <GitBranch size={14} className="text-accent shrink-0" />
          <span className="text-[12px] font-medium text-text truncate">
            {statusLoading
              ? i18nT('components.gitPanel.loading')
              : status?.branch
                || (status?.repos?.length
                  ? i18nT('components.gitPanel.repos_count', { count: status.repos.length })
                  : '')}
          </span>

          {/* Ahead/behind pill */}
          {status && (status.ahead != null || status.behind != null) && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-bg-hover text-muted font-mono shrink-0">
              {status.ahead != null && <>&#x2191;{status.ahead}</>}
              {status.behind != null && <>{' '}&#x2193;{status.behind}</>}
            </span>
          )}

          <span className="flex-1" />

          {/* Uncommitted / clean pill */}
          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium shrink-0 ${isClean ? 'bg-ok/15 text-ok' : 'bg-warn/15 text-warn'}`}>
            {statusLoading
              ? '...'
              : isClean
                ? i18nT('components.gitPanel.clean')
                : i18nT('components.gitPanel.uncommitted', { count: fileCount })}
          </span>

          {/* Refresh */}
          <button
            onClick={() => { forceFresh.current = true; refetchStatus(); refetchLog() }}
            className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none"
            title={i18nT('components.gitPanel.refresh')}
            aria-label={i18nT('components.gitPanel.refresh')}
          >
            <RefreshCw size={13} />
          </button>
        </div>
      }
    >
      <div className="overflow-y-auto flex-1 text-[12px]">
        {/* ── CHANGES section ── */}
        {(!isClean || status?.reposTruncated || status?.refused || groups.some(g => g.refused)) && (
          <section className="py-2">
            <div className="px-3 pb-1.5 flex items-center gap-1.5">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">
                {i18nT('components.gitPanel.changes')}
              </span>
              <span className="text-[10px] text-muted">{fileCount}</span>
            </div>
            <div>
              {groups.map(group => (
                <div key={group.root}>
                  {group.name && (
                    <div className="flex items-center gap-1.5 px-3 py-1 bg-bg-elevated border-y border-border">
                      <FolderGit2 className="lucide-inline shrink-0 text-muted" />
                      <span className="truncate text-[11px] text-text-strong font-medium" title={group.root}>{group.name}</span>
                      {group.branch && (
                        <span className="flex items-center gap-1 shrink-0 text-[10px] text-muted">
                          <GitBranch className="lucide-inline" />
                          <span className="truncate max-w-[110px]">{group.branch}</span>
                        </span>
                      )}
                      <span className="flex-1" />
                      {group.refused
                        ? (
                          <span
                            className="text-[10px] text-muted italic"
                            title={i18nT('components.gitPanel.repo_skipped_reason')}
                          >
                            {i18nT('components.gitPanel.repo_skipped')}
                          </span>
                        )
                        : group.truncated
                          ? (
                            <span
                              className="text-[10px] text-muted tabular-nums"
                              title={i18nT('components.gitPanel.truncated', { count: fileCount })}
                            >
                              &mdash;
                            </span>
                          )
                          : <span className="text-[10px] text-muted tabular-nums">{group.files.length}</span>}
                    </div>
                  )}
                  {group.files.map(f => (
                    <button
                      key={`${group.root}:${f.path}:${f.staged}`}
                      className="w-full flex items-center gap-2 px-3 py-1 hover:bg-bg-hover cursor-pointer transition-colors bg-transparent border-none text-left"
                      // Paths are repo-root-relative; anchor them to the row's
                      // OWN repo root so the open cannot resolve against a
                      // sibling repo, or against a DIFFERENT project's dir
                      // (the gateway-wide project) when slots differ.
                      onClick={() => {
                        const owner = f.repoRoot ?? group.root
                        onFileOpen?.(owner ? `${owner}/${f.path}` : f.path)
                      }}
                      title={f.path}
                    >
                      <span className={`font-mono font-semibold w-[14px] text-center shrink-0 ${statusColor(f.status)}`}>
                        {f.status}
                      </span>
                      <span className="flex-1 truncate">
                        <FilePath path={f.path} />
                      </span>
                      {(f.additions != null || f.deletions != null) && (
                        <span className="font-mono text-[11px] shrink-0">
                          {f.additions != null && f.additions > 0 && <span className="text-[var(--diff-add-text,var(--ok))]">+{f.additions}</span>}
                          {f.deletions != null && f.deletions > 0 && <span className="text-[var(--diff-del-text,var(--danger))] ml-1">-{f.deletions}</span>}
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              ))}
              {status?.truncated && (
                <div className="px-3 py-1 border-t border-border text-[10px] text-muted text-right">
                  {i18nT('components.gitPanel.truncated', { count: fileCount })}
                </div>
              )}
              {status?.reposTruncated && (
                <div className="px-3 py-1 border-t border-border text-[10px] text-muted text-right">
                  {i18nT('components.gitPanel.repos_truncated')}
                </div>
              )}
              {(status?.refused || groups.some(g => g.refused)) && (
                <div className="px-3 py-1 border-t border-border text-[10px] text-muted text-right">
                  {i18nT('components.gitPanel.repo_skipped_reason')}
                </div>
              )}
            </div>
          </section>
        )}

        {/* ── COMMITS section ── */}
        {!log?.commits?.length && !!status?.repos?.length && (
          <section className="py-2 border-t border-border">
            <div className="px-3 pb-1.5">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">
                {i18nT('components.gitPanel.commits')}
              </span>
            </div>
            <div className="px-3 text-[11px] text-muted">
              {i18nT('components.gitPanel.commits_unavailable')}
            </div>
          </section>
        )}

        {log?.commits && log.commits.length > 0 && (
          <section className="py-2 border-t border-border">
            <div className="px-3 pb-1.5">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">
                {i18nT('components.gitPanel.commits')}
              </span>
            </div>
            <div>
              {log.commits.map(c => (
                <div key={c.sha} className="px-3 py-1.5 hover:bg-bg-hover transition-colors">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[11px] text-accent shrink-0">{c.sha.slice(0, 7)}</span>
                    <span className="truncate text-text">{c.message.split('\n')[0]}</span>
                  </div>
                  <div className="text-[11px] text-muted mt-0.5 flex items-center gap-1.5">
                    <span>{c.author}</span>
                    <span>-</span>
                    <span>{relativeTime(c.date)}</span>
                    {c.isHead && <span className="text-accent font-semibold">HEAD</span>}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* Empty state -- suppressed for a multi-repo project, whose Commits
            section already explains why it carries no history. */}
        {isClean && (!log?.commits || log.commits.length === 0) && !statusLoading
          && !status?.repos?.length && (
          <div className="px-3 py-8 text-center text-muted text-[12px]">
            {i18nT('components.gitPanel.empty_state')}
          </div>
        )}
      </div>
    </DetailPanel>
  )
}
