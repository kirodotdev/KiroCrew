import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { GitBranch, RefreshCw, RefreshCwOff } from 'lucide-react'
import { api } from '../api/client'
import DetailPanel from './DetailPanel'
import ErrorNotice from './ErrorNotice'
import { errMessage } from '../utils/thunkError'
import { findReport } from '../utils/errorReport'
import {
  gitErrorCode,
  gitFilterRefusalCause,
  gitFilterRefusalCopyKey,
  gitFilterRefusalScope,
  gitFilterRefusalTitleKey,
} from '../utils/gitStatusError'
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

  const { data: status, refetch: refetchStatus, isLoading: statusLoading, error: statusError } = useQuery({
    queryKey: ['git-status', projectDir],
    queryFn: () => api.projectGitStatus(projectDir),
    enabled: !!projectDir,
    refetchInterval: 5000,
    refetchOnWindowFocus: true,
    retry: 1,
  })

  const { data: log, refetch: refetchLog, error: logError } = useQuery({
    queryKey: ['git-log', projectDir],
    queryFn: () => api.projectGitLog(projectDir),
    enabled: !!projectDir,
    staleTime: 30_000,
    retry: 1,
  })

  // A failing git backend used to render an EMPTY panel — no changes, no
  // commits — indistinguishable from a clean repo with no history. Each read
  // failure now names itself; the panel holds no draft, so the agent hand-off
  // is offered (a git that is missing or a dir that is not a repo is exactly
  // what it can diagnose).

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
  const isRepository = status?.repo === true
  const noRepository = !statusError && status?.repo === false
  const hasNoChanges = fileCount === 0
  const isClean = !statusError && isRepository && hasNoChanges
  // The server caps the listing and says so. Unless the panel reads that flag
  // the cap reads as the total, so a repo with 900 changed files shows "500
  // uncommitted" and its list simply ends -- an undercount presented as a
  // count.
  const listTruncated = !statusError && isRepository && status?.truncated === true
  const statusErrorMessage = errMessage(statusError)
  const statusUnavailable = gitErrorCode(statusError) === 'git_status_unavailable'
  // A coded log refusal renders the localized refusal copy, the same as the
  // coalesced notice does -- it is the same condition, and the backend's own
  // English sentence under a localized title reads as mixed-language copy in the
  // other twelve catalogs. An uncoded failure keeps its message, which is then
  // the only detail there is.
  const logErrorMessage = errMessage(logError)
  const localizedLogFailure = i18nT('components.gitPanel.log_failed')
  // A repo whose own config declares a filter driver is refused by
  // policy, not by an outage: the cause is knowable, it is permanent for that
  // repo, and no retry clears it. Both routes refuse together because the cause
  // is repo-level, so this is ONE notice naming the cause rather than two that
  // each report a generic failure the reader cannot act on.
  const statusFilterRefused = gitErrorCode(statusError) === 'git_status_filter_refused'
  const logFilterRefused = gitErrorCode(logError) === 'git_log_filter_refused'
  // Coalesce into one notice ONLY when every failure present is the refusal.
  // The cause is repo-level so the routes normally refuse together, and two
  // boxes would report one condition twice -- but the routes CAN fail
  // divergently (a corrupt HEAD gives status `git_status_unavailable` while the
  // log route still refuses), and collapsing then would hide a real, separate
  // failure behind the refusal's copy. Suppressing one true condition because a
  // sibling reported another is the same kind of wrong answer this panel is
  // being fixed for.
  const filterRefused =
    (statusFilterRefused || logFilterRefused) &&
    !(statusError && !statusFilterRefused) &&
    !(logError && !logFilterRefused)
  // Safe by that invariant: inside this branch every present error IS a
  // refusal, so whichever message is picked carries a refusal code.
  const filterRefusedMessage = statusErrorMessage || logErrorMessage
  // One condition, two causes, and the reader gets the one that holds. A
  // disjunction ("declares a driver, or couldn't be read") made every LFS repo
  // -- the common case, on every visit -- read a question the backend had
  // already answered. Unknown/absent cause falls back to the unreadable copy,
  // which claims strictly less.
  const filterRefusedCause =
    gitFilterRefusalCause(statusFilterRefused ? statusError : logError)
  const filterRefusedCopy = i18nT(gitFilterRefusalCopyKey(filterRefusedCause))
  // What the refusal accounts for, which is NOT always both halves. Two states
  // leave one route refusing on its own: the divergent one, where the sibling
  // route failed for its own reason and carries its own notice; and a recovered
  // one, where the other route is simply healthy and its list is rendering
  // underneath. A refusal that claims both halves there either reports the
  // sibling notice's failure as its own or denies a list on screen -- the same
  // over-claim this panel already avoids on the status side by dropping the
  // stale-history clause when the log has its own notice. The title carries the
  // scope, so the body stays one sentence per cause.
  const filterRefusedScope = gitFilterRefusalScope(statusFilterRefused, logFilterRefused)
  const filterRefusedTitle = i18nT(
    gitFilterRefusalTitleKey(filterRefusedCause, filterRefusedScope),
  )
  // "Commit history may be out of date" is a promise about a list that is still
  // rendered. When the log route ALSO failed there is no list, and the notice
  // below says so outright, so the two boxes then contradict each other -- one
  // warning about staleness in something the other says cannot be shown. Drop
  // the clause exactly when the log side has its own notice.
  const localizedStatusFailure = i18nT(
    logError
      ? 'components.gitPanel.status_failed_no_history'
      : 'components.gitPanel.status_failed',
  )
  // The refusal is permanent only while the repo config stands, and the
  // notice's own agent hand-off invites changing it in this window. The status
  // query recovers on its own 5 s interval; the log query has no interval and a
  // 30 s staleTime, and the branch-change effect above cannot fire on the first
  // successful status (it needs a PREVIOUS marker, which a refusing status never
  // set). Its cached refusal would therefore hold `filterRefused` true by
  // itself, keeping a notice up that says history can't be shown while the
  // changes list renders under it. Refetch the log the moment the status route
  // comes back CLEAN -- not merely stops refusing. A status route that fails for
  // its own reason (the divergent case) proves nothing about the repo config, so
  // refetching there would churn a query whose refusal is still current.
  useEffect(() => {
    if (!statusError && logFilterRefused) {
      refetchLog()
    }
  }, [statusError, logFilterRefused, refetchLog])
  // Inert ONLY while both routes are still refusing AND the cause is a declared
  // driver, which is the standing policy state a retry cannot change. An
  // unreadable config can become readable, and the status route re-polls every
  // 5 s, so a retry there can genuinely succeed -- promising otherwise, or
  // deadening the control, would be the same over-claim this panel is being
  // fixed for. Once either route has recovered, the button is also the manual
  // escape from the other's cached refusal.
  const refreshInert =
    statusFilterRefused && logFilterRefused && filterRefusedCause === 'declared'

  return (
    <DetailPanel
      embedded
      title={i18nT('components.gitPanel.title')}
      onClose={onClose}
      noPadding
      customHeader={
        <div className="flex items-center gap-2 h-[38px] px-3 shrink-0 border-b border-border">
          {statusError ? (
            <span className="text-[12px] text-muted truncate">
              {i18nT('components.gitPanel.branch_unavailable')}
            </span>
          ) : (
            <>
              {/* Branch name */}
              <GitBranch size={14} className="text-accent shrink-0" />
              <span className="text-[12px] font-medium text-text truncate">
                {status?.branch || (noRepository
                  ? i18nT('components.gitPanel.not_a_repository')
                  : i18nT('components.gitPanel.loading'))}
              </span>

              {/* Ahead/behind pill */}
              {status && (status.ahead != null || status.behind != null) && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-bg-hover text-muted font-mono shrink-0">
                  {status.ahead != null && <>&#x2191;{status.ahead}</>}
                  {status.behind != null && <>{' '}&#x2193;{status.behind}</>}
                </span>
              )}
            </>
          )}

          <span className="flex-1" />

          {/* Uncommitted / clean pill */}
          {!noRepository && !statusError && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium shrink-0 ${hasNoChanges ? 'bg-ok/15 text-ok' : 'bg-warn/15 text-warn'}`}>
              {statusLoading
                ? '...'
                : hasNoChanges
                  ? i18nT('components.gitPanel.clean')
                  : listTruncated
                    ? i18nT('components.gitPanel.uncommitted_capped', { count: fileCount })
                    : i18nT('components.gitPanel.uncommitted', { count: fileCount })}
            </span>
          )}

          {/* Refresh. Inert under a filter-driver refusal: the copy right below
              says retrying won't help, and a live button over that sentence
              contradicts it. The refusal is a standing policy decision keyed on
              repo config, so a re-fetch returns the same 503 every time. Kept
              in place rather than hidden so the header does not reflow.
              Dimming alone was NOT enough -- a reader still could not tell
              whether pressing it did anything and said they would press it
              anyway -- so the glyph itself changes to the slashed variant. The
              shape carries the state without depending on a hover tooltip or
              on noticing a contrast difference. Keyed on BOTH routes refusing,
              never on the coalescing predicate: a half-recovered pair needs
              this button as its escape.

              The inert treatment deviates from the repo's dominant `opacity-40`
              disabled skin, at `opacity-60` and a heavier stroke, and that is
              deliberate. Everywhere else a disabled glyph carries no
              information -- the fade IS the message -- so 40% costs nothing.
              Here the whole difference between the two refusal causes is one
              thin diagonal at `size={13}`, and a reader shown three frames
              called all three crossed-out. The slash is the first thing 40%
              erases. Weaker than the live glyph either way, so the resting
              convention below is untouched. */}
          <button
            onClick={() => { refetchStatus(); refetchLog() }}
            disabled={refreshInert}
            className={`flex items-center justify-center w-[26px] h-[26px] rounded-md transition-colors bg-transparent border-none ${
              refreshInert
                ? 'text-muted opacity-60 cursor-not-allowed'
                : 'cursor-pointer text-muted hover:text-text hover:bg-bg-hover'
            }`}
            title={refreshInert
              ? i18nT('components.gitPanel.refresh_unavailable')
              : i18nT('components.gitPanel.refresh')}
            aria-label={refreshInert
              ? i18nT('components.gitPanel.refresh_unavailable')
              : i18nT('components.gitPanel.refresh')}
          >
            {refreshInert ? <RefreshCwOff size={13} strokeWidth={2.5} /> : <RefreshCw size={13} />}
          </button>
        </div>
      }
    >
      <div className="overflow-y-auto flex-1 text-[12px]">
        {(statusError || logError) && (
          <div className="flex flex-col gap-2 p-3">
            {/* The refusal is repo-level, so both routes refuse together: ONE
                notice naming the cause, not two reporting a generic failure.
                It goes through ErrorNotice like every other error value here
                (AUTOSDE errors-use-error-notice) -- the string originates in a
                503 {error, code} body, and the agent hand-off is the point: a
                filter-driver config is exactly what it can explain or change. */}
            {filterRefused ? (
              <ErrorNotice
                title={filterRefusedTitle}
                message={filterRefusedCopy}
                report={findReport(filterRefusedMessage)}
                askAgent
                testId="git-panel-filter-refused"
              />
            ) : (
              <>
                {statusError && (
                  <ErrorNotice
                    // The outage notice is deliberately UNTITLED. A title here
                    // restated its own first clause, and both notices then
                    // opened on the same word, so the refusal's permanence and
                    // this one's transience were a body-read apart. Titled vs
                    // untitled tells them apart at a glance instead. A coded
                    // REFUSAL is the exception: it is the same condition the
                    // coalesced notice names, so it gets that title and that
                    // localized copy -- the backend's English sentence under a
                    // localized heading is mixed-language copy in the other
                    // twelve catalogs, the same defect already fixed on the log
                    // side.
                    title={
                      statusFilterRefused
                        ? filterRefusedTitle
                        : !statusUnavailable && statusErrorMessage
                          ? localizedStatusFailure
                          : undefined
                    }
                    message={
                      statusFilterRefused
                        ? filterRefusedCopy
                        : statusUnavailable
                          ? localizedStatusFailure
                          : statusErrorMessage || localizedStatusFailure
                    }
                    report={findReport(statusErrorMessage)}
                    askAgent
                    // Two notices stack here, each carrying its own report. With
                    // the shared label nothing said which failure a hand-off
                    // was for, so both read as one affordance rendered twice.
                    askAgentLabel={i18nT('components.gitPanel.ask_agent_changes')}
                    testId="git-panel-status-error"
                  />
                )}
                {logError && (
                  <ErrorNotice
                    title={
                      logFilterRefused
                        ? filterRefusedTitle
                        : logErrorMessage ? localizedLogFailure : undefined
                    }
                    message={
                      logFilterRefused
                        ? filterRefusedCopy
                        : logErrorMessage || localizedLogFailure
                    }
                    // Explicit, because the hand-off otherwise resolves context
                    // by looking the MESSAGE up in the error journal, and a
                    // localized message cannot match the backend's raw string.
                    // Without it the agent gets the sentence and no endpoint,
                    // status or code.
                    report={findReport(logErrorMessage)}
                    askAgent
                    askAgentLabel={i18nT('components.gitPanel.ask_agent_history')}
                    testId="git-panel-log-error"
                  />
                )}
              </>
            )}
          </div>
        )}

        {noRepository && (
          <div role="status" className="px-3 py-8 text-center text-muted text-[12px]">
            {i18nT('components.gitPanel.not_a_repository_help')}
          </div>
        )}

        {/* ── CHANGES section ── */}
        {!statusError && isRepository && !hasNoChanges && (
          <section className="py-2">
            <div className="px-3 pb-1.5 flex items-center gap-1.5">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">
                {i18nT('components.gitPanel.changes')}
              </span>
              {/* One claim per number: when the listing is capped the note below
                  carries the count AND qualifies it, so a bare total beside it
                  would state the same number twice in three words. */}
              {listTruncated ? (
                <span role="status" className="text-[10px] text-warn" data-testid="git-panel-truncated">
                  {i18nT('components.gitPanel.showing_first', { count: fileCount })}
                </span>
              ) : (
                <span className="text-[10px] text-muted">{fileCount}</span>
              )}
            </div>
            <div>
              {status?.files.map(f => (
                <button
                  key={`${f.path}:${f.staged}`}
                  className="w-full flex items-center gap-2 px-3 py-1 hover:bg-bg-hover cursor-pointer transition-colors bg-transparent border-none text-left"
                  // Paths are repo-root-relative; anchor them to the repo root
                  // so the open cannot resolve against a DIFFERENT project's
                  // dir (the gateway-wide project) when several slots have
                  // different projects set.
                  onClick={() => onFileOpen?.(status.repoRoot ? `${status.repoRoot}/${f.path}` : f.path)}
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
          </section>
        )}

        {/* ── COMMITS section ── */}
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

        {/* Empty state */}
        {isClean && (!log?.commits || log.commits.length === 0) && !statusLoading && (
          <div className="px-3 py-8 text-center text-muted text-[12px]">
            {i18nT('components.gitPanel.empty_state')}
          </div>
        )}
      </div>
    </DetailPanel>
  )
}
