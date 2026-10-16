import { Fragment, useEffect, useId, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ChevronDown, ChevronRight, Cloud, GitBranch, RefreshCw } from 'lucide-react'
import { api, type RemoteGitResult } from '../../api/client'
import AskAgentButton from '../../components/AskAgentButton'
import ErrorNotice from '../../components/ErrorNotice'
import { relativeTime, statusColor } from '../../components/GitPanel'
import StatusNotice from '../../components/StatusNotice'
import { slotEnvironmentQueryKey, useSlotEnvironmentPlacement } from '../../components/sessionEnvironmentHooks'
import { Btn, IconButton, PanelSectionHeader } from '../../components/ui'
import { fmtList } from '../../i18n/format'
import { PierrePatch } from '../../pierre'
import { errorCode, isCloudSandbox, remoteKey } from './RemoteFilesPanel'

/** `from` is a rename or copy's source path; `path` is where it is now. */
interface StatusRow { code: string; path: string; from?: string }
interface Porcelain { branch: string; ahead: number | null; behind: number | null; rows: StatusRow[] }

/** One status letter per row, as the local Git panel shows it: `??` is `?`. */
export function statusLetter(code: string): string {
  if (code === '??') return '?'
  return code.trim().charAt(0) || '·'
}

/** `## main...origin/main [ahead 1, behind 2]` -> the local branch and counts;
 *  the counts are null without an upstream, which the pill then says. */
function parseBranch(line: string): Pick<Porcelain, 'branch' | 'ahead' | 'behind'> {
  // An unborn branch keeps its `...upstream [gone]` suffix: git cannot count
  // commits against a branch that has none, even when the upstream exists.
  const unborn = /^No commits yet on (.+)$/.exec(line)
  if (unborn) return { branch: unborn[1].split('...', 1)[0], ahead: null, behind: null }
  const [head, upstream] = line.split('...', 2)
  if (upstream === undefined) return { branch: head, ahead: null, behind: null }
  const counts = /\[([^\]]*)\]$/.exec(upstream)?.[1] ?? ''
  if (counts === 'gone') return { branch: head, ahead: null, behind: null }
  const count = (word: string) => Number(new RegExp(`${word} (\\d+)`).exec(counts)?.[1] ?? 0)
  return { branch: head, ahead: count('ahead'), behind: count('behind') }
}

/** `git status --porcelain=v1 -z --branch` -> branch + literal paths. A guest
 *  worker without NUL status support answers with newline records, read too.
 *  `truncated` marks output cut at the read cap, which can end mid-record:
 *  everything after the last separator is dropped rather than shown as a path. */
export function parsePorcelain(output: string, truncated = false): Porcelain {
  let branch: Pick<Porcelain, 'branch' | 'ahead' | 'behind'> = { branch: '', ahead: null, behind: null }
  const rows: StatusRow[] = []
  const nul = output.includes('\0')
  const separator = nul ? '\0' : '\n'
  const complete = truncated ? output.slice(0, output.lastIndexOf(separator) + 1) : output
  const records = complete.split(separator)
  for (let i = 0; i < records.length; i++) {
    const record = records[i]
    if (!record) continue
    if (record.startsWith('## ')) { branch = parseBranch(record.slice(3)); continue }
    const code = record.slice(0, 2)
    const moved = code.includes('R') || code.includes('C')
    if (nul) {
      // NUL porcelain emits the destination first and the source in its own record.
      const from = moved ? records[++i] : undefined
      rows.push(from ? { code, path: record.slice(3), from } : { code, path: record.slice(3) })
      continue
    }
    // Newline porcelain writes a move as `source -> destination` in one record.
    const arrow = moved ? record.indexOf(' -> ', 3) : -1
    rows.push(arrow >= 0
      ? { code, path: record.slice(arrow + 4), from: record.slice(3, arrow) }
      : { code, path: record.slice(3) })
  }
  return { ...branch, rows }
}

/** Git's own wording for a workdir outside any repository. The current guest
 *  worker answers that case as `{git: false}`; a worker from an earlier build
 *  passes git's stderr through as `error_text` with no `code`, and uses the
 *  same code-less shape for every other failure ("git is not installed",
 *  "dubious ownership"), so only this text is read as not-a-repository. */
const NOT_A_REPOSITORY = /^fatal: not a git repository\b/m

/** A non-repository reply. A `git_failed` result is a failure whatever its text. */
export function isNotRepository(data: RemoteGitResult): boolean {
  if (data.git || data.code) return false
  return !data.error_text || NOT_A_REPOSITORY.test(data.error_text)
}

/** A commit's date: the guest worker sends unix seconds, formatted here as
 *  the local Git panel formats its own. A value no date can hold is shown as
 *  it came. */
function commitWhen(when: string | undefined): string {
  const seconds = Number(when)
  const date = new Date(seconds * 1000)
  if (!when || !Number.isFinite(seconds) || !Number.isFinite(date.getTime())) return when ?? ''
  return relativeTime(date.toISOString())
}

export default function RemoteGitPanel(props: { slot: string; name: string }) {
  const { t } = useTranslation()
  const { remote, error } = useSlotEnvironmentPlacement(props.slot)
  if (error) return <div className="p-3"><ErrorNotice message={t('sessionEnvironments.loadError')} askAgent /></div>
  if (remote?.provider === 'kiro_cloud') {
    return <div className="p-3"><StatusNotice tone="info" message={t('sessionEnvironments.remoteFiles.cloudSandbox')} /></div>
  }
  if (remote === undefined) return <div className="p-3" aria-busy="true">{t('sessionEnvironments.remoteFiles.loading')}</div>
  return <RemoteGitView key={props.slot} {...props} />
}

/** The Git view for a session on a REMOTE environment: the guest workdir's
 *  status, history and per-file diff, read-only, through the remote browse
 *  endpoint. The local Git panel would describe the gateway's checkout.
 *  Keyed on the slot: the Git tab stays mounted across chat switches, and an
 *  open file belongs to the session it was opened in. */
function RemoteGitView({ slot, name }: { slot: string; name: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  // Only one row is open at a time, so one id names the open diff region.
  const diffId = `${useId()}-diff`
  const status = useQuery({
    queryKey: [...remoteKey(slot), 'git', 'status'],
    queryFn: () => api.chatSlotEnvironmentGit(slot, 'git_status'),
    retry: false,
    staleTime: 10_000,
  })
  // A cloud refusal means the placement this panel mounted on is stale: re-read
  // it once per refusal, so the wrapper swaps to the neutral notice (the Files
  // panel's rule).
  const cloudSandbox = status.isError && isCloudSandbox(status.error)
  const refusedAt = cloudSandbox ? status.errorUpdatedAt : 0
  useEffect(() => {
    if (refusedAt) void queryClient.invalidateQueries({ queryKey: slotEnvironmentQueryKey(slot) })
  }, [queryClient, slot, refusedAt])
  const log = useQuery({
    queryKey: [...remoteKey(slot), 'git', 'log'],
    queryFn: () => api.chatSlotEnvironmentGit(slot, 'git_log'),
    retry: false,
    staleTime: 30_000,
    enabled: !!status.data?.git,
  })
  // A rename's diff reads its HEAD side at the old path; a copy shows as the
  // new file it is.
  const selectedFrom = selected && status.data?.git
    ? parsePorcelain(status.data.output ?? '', !!status.data.truncated).rows
      .find((row) => row.path === selected && row.code.includes('R'))?.from
    : undefined
  const diff = useQuery({
    queryKey: [...remoteKey(slot), 'git', 'diff', selected, selectedFrom ?? null],
    queryFn: () => selectedFrom
      ? api.chatSlotEnvironmentGit(slot, 'git_diff', selected ?? '.', selectedFrom)
      : api.chatSlotEnvironmentGit(slot, 'git_diff', selected ?? '.'),
    retry: false,
    enabled: !!selected && !!status.data?.git,
  })
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: [...remoteKey(slot), 'git'] }) }
  /** A request to the agent, staged as a composer draft; the label says so. */
  const askAgent = (prompt: string, label: string) => (
    <AskAgentButton prompt={prompt} label={label} title={t('sessionEnvironments.remoteGit.askDraftTitle')} tone="accent" />
  )
  /** A failed read leads with what could not be read; git's own text, or the
   *  transport failure, follows as the detail. A coded stop is already a
   *  product sentence and stands alone. */
  const failure = (error: unknown, data: RemoteGitResult | undefined, lead: string, inline: boolean) => {
    if (errorCode(error) === 'workspace_stopped') {
      return <ErrorNotice variant={inline ? 'inline' : 'block'} message={t('sessionEnvironments.remoteFiles.stopped')} askAgent />
    }
    const gitText = data?.error_text
    const detail = gitText || (data?.code === 'git_failed' ? '' : t('sessionEnvironments.remoteFiles.error'))
    return (
      <ErrorNotice
        variant={inline ? 'inline' : 'block'}
        title={detail ? lead : undefined}
        message={detail || lead}
        messagePlacement="below"
        messageClassName={gitText ? 'font-mono' : ''}
        askAgent
      />
    )
  }

  const header = (
    <div className="flex items-center gap-2 px-3 h-9 border-b border-border shrink-0">
      <Cloud size={14} className="shrink-0 text-accent" aria-hidden />
      <span className="text-[12.5px] text-text truncate flex-1">{t('sessionEnvironments.remoteGit.title', { name })}</span>
      <IconButton aria-label={t('sessionEnvironments.remoteFiles.refresh')} title={t('sessionEnvironments.remoteFiles.refresh')} onClick={refresh}>
        <RefreshCw className="lucide-inline" />
      </IconButton>
    </div>
  )
  if (cloudSandbox) {
    return (
      <div className="h-full flex flex-col">
        {header}
        <div className="p-3"><ErrorNotice message={t('sessionEnvironments.remoteFiles.cloudSandbox')} askAgent /></div>
      </div>
    )
  }
  if (status.isError || (status.data && !isNotRepository(status.data) && (status.data.code === 'git_failed' || status.data.error_text))) {
    return <div className="h-full flex flex-col">{header}<div className="p-3">{failure(status.error, status.data, t('sessionEnvironments.remoteGit.statusFailed'), false)}</div></div>
  }
  if (!status.data) {
    return <div className="h-full flex flex-col">{header}<div className="p-3 text-[12.5px] text-muted" aria-busy="true">{t('sessionEnvironments.remoteFiles.loading')}</div></div>
  }
  if (!status.data.git) {
    return (
      <div className="h-full flex flex-col">
        {header}
        <div className="p-6 flex flex-col items-center gap-2 text-center text-[13px] text-muted">
          <span>{t('sessionEnvironments.remoteGit.notRepo')}</span>
          {askAgent(t('sessionEnvironments.remoteGit.setUpRepoPrompt', { name }), t('sessionEnvironments.remoteGit.askSetUpRepo'))}
        </div>
      </div>
    )
  }
  const { branch, ahead, behind, rows } = parsePorcelain(status.data.output ?? '', !!status.data.truncated)
  // Every status row claims a change; a selection the latest status no longer
  // lists (it was refreshed clean) does not.
  const selectedListed = rows.some((row) => row.path === selected)
  const syncLabel = fmtList([
    ahead != null ? t('sessionEnvironments.remoteGit.notPushed', { count: ahead }) : '',
    behind != null ? t('sessionEnvironments.remoteGit.notPulled', { count: behind }) : '',
  ], { type: 'unit', style: 'short' })
  const statusTitle = (letter: string) => {
    switch (letter) {
      case 'M': return t('sessionEnvironments.remoteGit.statusModified')
      case 'A': return t('sessionEnvironments.remoteGit.statusAdded')
      case 'R': return t('sessionEnvironments.remoteGit.statusRenamed')
      case 'C': return t('sessionEnvironments.remoteGit.statusCopied')
      case 'D': return t('sessionEnvironments.remoteGit.statusDeleted')
      case '?': return t('sessionEnvironments.remoteGit.statusUntracked')
      default: return t('sessionEnvironments.remoteGit.statusOther')
    }
  }
  const logFailed = log.isError || log.data?.code === 'git_failed' || !!log.data?.error_text
  /** The open file's diff and its notices, shown directly under its row. */
  const diffView = (path: string) => (
    diff.isError || diff.data?.code === 'git_failed' || diff.data?.error_text ? (
      failure(diff.error, diff.data, t('sessionEnvironments.remoteGit.diffFailed'), true)
    ) : diff.data ? (
      // A cut reply can carry an empty patch when every change lies past the
      // cut, so it is never read as "no difference".
      diff.data.output || diff.data.truncated ? (
        <>
          {diff.data.truncated && (
            <div className="py-1 flex flex-wrap items-center gap-x-2 text-[12px] text-muted" role="note">
              <span>{t('sessionEnvironments.remoteGit.truncatedDiff')}</span>
              {askAgent(t('sessionEnvironments.remoteGit.fullDiffPrompt', { path, name }), t('sessionEnvironments.remoteGit.askFullDiff'))}
            </div>
          )}
          {diff.data.output && <PierrePatch patch={diff.data.output} />}
        </>
      ) : (
        <div className="py-2 text-[12.5px] text-muted">
          {t(selectedListed ? 'sessionEnvironments.remoteGit.emptyDiffListed' : 'sessionEnvironments.remoteGit.noDiff')}
        </div>
      )
    ) : (
      <div className="py-2 text-[12.5px] text-muted" aria-busy="true">{t('sessionEnvironments.remoteFiles.loading')}</div>
    )
  )
  const logRead = !!log.data && !logFailed
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
          <span className="truncate" title={t('sessionEnvironments.remoteGit.branchTitle')}>{branch}</span>
          {/* Plain muted text, not a pill: neither reading is a control. */}
          {ahead != null || behind != null ? (
            <span title={t('sessionEnvironments.remoteGit.syncTitle', { counts: syncLabel })} className="text-[11.5px] text-muted font-mono shrink-0">
              <span aria-hidden="true">
                {ahead != null && <>&#x2191;{ahead}</>}
                {behind != null && <>{' '}&#x2193;{behind}</>}
              </span>
              <span className="sr-only">{syncLabel}</span>
            </span>
          ) : (
            <span title={t('sessionEnvironments.remoteGit.noUpstreamTitle')} className="text-[11.5px] text-muted shrink-0">
              {t('sessionEnvironments.remoteGit.noUpstream')}
            </span>
          )}
        </div>
        <div>
          <PanelSectionHeader
            label={t('sessionEnvironments.remoteGit.changes')}
            count={status.data.truncated ? t('sessionEnvironments.remoteGit.changesCountCapped', { count: rows.length }) : rows.length}
          />
          {status.data.truncated && (
            <div className="py-1 flex flex-wrap items-center gap-x-2 text-[12px] text-muted" role="note">
              <span>{t('sessionEnvironments.remoteGit.statusTruncated')}</span>
              {askAgent(t('sessionEnvironments.remoteGit.fullStatusPrompt', { name }), t('sessionEnvironments.remoteGit.askFullStatus'))}
            </div>
          )}
          {rows.length === 0 ? (
            <div className="py-2 text-[12.5px] text-muted">{t('sessionEnvironments.remoteGit.clean')}</div>
          ) : (
            <div className="mt-1 flex flex-col">
              {rows.map((row) => {
                const letter = statusLetter(row.code)
                const open = selected === row.path
                return (
                  <Fragment key={row.path}>
                    <Btn
                      type="button"
                      aria-expanded={open}
                      aria-controls={open ? diffId : undefined}
                      onClick={() => setSelected(open ? null : row.path)}
                      className={`group justify-start gap-2 h-7 rounded-md border-none bg-transparent text-[12.5px] font-normal hover:bg-bg-hover ${open ? 'bg-bg-hover' : ''}`}
                    >
                      <span title={statusTitle(letter)} className={`font-mono font-semibold w-[14px] text-center shrink-0 ${statusColor(letter)}`}>
                        <span aria-hidden="true">{letter}</span>
                        {!row.from && <span className="sr-only">{statusTitle(letter)}</span>}
                      </span>
                      {row.from ? (
                        <span className="font-mono truncate">
                          <span aria-hidden="true">{row.from} &#x2192; {row.path}</span>
                          <span className="sr-only">
                            {t(letter === 'C' ? 'sessionEnvironments.remoteGit.copiedFrom' : 'sessionEnvironments.remoteGit.renamedFrom', { from: row.from, path: row.path })}
                          </span>
                        </span>
                      ) : (
                        <span className="font-mono truncate">{row.path}</span>
                      )}
                      {/* The open row spells its letter out; the sr-only label
                          above already names it to a screen reader. */}
                      {open && (
                        <span aria-hidden="true" className="ml-auto pl-2 shrink-0 text-[11.5px] text-muted">{statusTitle(letter)}</span>
                      )}
                      {/* Every row opens its diff directly below it; the chevron
                          says so before the first click, as the Files tab's folders do. */}
                      {open
                        ? <ChevronDown className="lucide-inline shrink-0 text-muted" aria-hidden />
                        : <ChevronRight className="lucide-inline shrink-0 ml-auto text-muted group-hover:text-text" aria-hidden />}
                    </Btn>
                    {open && <div id={diffId} data-testid="remote-git-diff" className="shrink-0 min-w-0 py-1">{diffView(row.path)}</div>}
                  </Fragment>
                )
              })}
            </div>
          )}
          {/* A selection a refresh no longer lists has no row to open under. */}
          {selected && !selectedListed && <div data-testid="remote-git-diff" className="shrink-0 min-w-0 py-1">{diffView(selected)}</div>}
        </div>
        <div>
          {/* No count until the log has been read: a failed read is not an empty history. */}
          <PanelSectionHeader label={t('sessionEnvironments.remoteGit.history')} count={logRead ? commits.length : undefined} />
          {logFailed && failure(log.error, log.data, t('sessionEnvironments.remoteGit.logFailed'), true)}
          <div className="mt-1 flex flex-col gap-1">
            {commits.map((c) => (
              <div key={c.hash} className="flex items-baseline gap-2 text-[12.5px] min-w-0">
                <span className="font-mono text-[11.5px] text-muted shrink-0">{c.hash}</span>
                <span className="truncate flex-1 text-text" title={c.subject}>{c.subject}</span>
                <span className="text-[11.5px] text-muted shrink-0">{commitWhen(c.when)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
