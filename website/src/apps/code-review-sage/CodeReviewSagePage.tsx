// Code Review Sage — dashboard page (/code-review-sage).
//
// GitHub-PR-only: paste one or more GitHub PR URLs and the app backend runs a
// deterministic two-stage review (POST /api/apps/code-review-sage/review). The
// driver runs in-process, so the Phase 1 -> Phase 2 switch and finalize always
// run. Findings post as a PENDING (draft) review the human submits on GitHub.
import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ScanSearch, GitPullRequest, ExternalLink, Circle, Settings, Brain, Plus, Trash2, FolderGit2, RefreshCw } from 'lucide-react'

import { SendBtn } from '../../components/ui'
import Clickable from '../../components/Clickable'

import { i18nT } from '../../i18n/t'
const API = '/api/apps/code-review-sage'

interface RunProgressEntry { phase: string; counts?: { red?: number; yellow?: number }; error?: string }
interface Run {
  run_id: string
  status: string
  changes: string[]
  change_ids?: string[]
  progress?: Record<string, RunProgressEntry>
  report_slug?: string | null
  error?: string
  started_at?: string
  finished_at?: string
}
interface Settings { model: string | null; effort: string; active_namespaces: string[]; max_concurrent?: number }
interface SettingsResp { settings: Settings; models: string[]; efforts: string[]; namespaces: string[]; max_concurrent_max?: number }

// A single open PR of a repo, annotated by the backend with dedup status.
interface RepoPr {
  url: string; number: number; title: string; head_sha: string
  change_id: string; reviewed: boolean; reviewed_stale: boolean; reviewed_at?: string
}
interface RepoPrsResp { repo: string; prs: RepoPr[]; count: number }

interface NamespaceInfo { name: string; patterns: number; candidate: number; active: boolean }
interface NamespacesResp { namespaces: NamespaceInfo[]; active: string[] }
interface Pattern { id: string; title: string; scope: string; impact: string; guidance: string }
interface LearningsResp { namespace: string; patterns: Pattern[]; candidate: Pattern[] }

// Human label for a GH-<owner>-<repo>-<n> change id (mirrors the backend id).
function changeLabel(id: string): string {
  const m = id.match(/^GH-(.+)-(.+)-(\d+)$/)
  return m ? `${m[1]}/${m[2]} #${m[3]}` : id
}

/**
 * Catalog KEY for each run-phase label.
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()`
 * call here would freeze the boot language and never re-resolve on a language
 * switch. The lookup happens in `phaseLabel()`, which runs during render.
 *
 * Flat `Record` of full literal keys indexed inline at the `i18nT()` call —
 * the only shape `scripts/check-i18n-keys.mjs` can resolve statically.
 */
const PHASE_LABEL_KEY: Record<string, string> = {
  queued: 'apps.codeReviewSage.codeReviewSagePage.phase_queued',
  gating: 'apps.codeReviewSage.codeReviewSagePage.phase_gating',
  deep: 'apps.codeReviewSage.codeReviewSagePage.phase_deep',
  done: 'apps.codeReviewSage.codeReviewSagePage.phase_done',
  failed: 'apps.codeReviewSage.codeReviewSagePage.phase_failed',
}

/**
 * Localised label for a run phase, or the phase id VERBATIM when the backend
 * reports one this table does not know — the same fallback the previous
 * `PHASE_LABEL[phase] ?? phase` gave, and better than fabricating copy.
 *
 * `hasOwnProperty`, not `in`: the phase comes off a backend progress payload,
 * so a value like `toString` would otherwise resolve to an inherited
 * Object.prototype member and hand a function to i18next.
 */
function phaseLabel(phase: string): string {
  return Object.prototype.hasOwnProperty.call(PHASE_LABEL_KEY, phase)
    ? i18nT(PHASE_LABEL_KEY[phase])
    : phase
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url)
  // Prefer the backend's {error} message (mirrors sendJSON) so failures like
  // "gh is not authenticated on the gateway host" reach the user, not "HTTP 502".
  const data = await r.json().catch(() => null)
  if (!r.ok || (data as { error?: string } | null)?.error) {
    throw new Error((data as { error?: string } | null)?.error || `HTTP ${r.status}`)
  }
  return data as T
}

async function sendJSON(url: string, body: unknown, method = 'POST'): Promise<Record<string, unknown>> {
  const r = await fetch(url, {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok || (data as { error?: string })?.error) {
    throw new Error((data as { error?: string })?.error || `HTTP ${r.status}`)
  }
  return data as Record<string, unknown>
}

export default function CodeReviewSagePage() {
  const qc = useQueryClient()
  const [input, setInput] = useState('')
  // Repo-review mode: enumerate a repo's open PRs and review the un-reviewed ones.
  const [repoUrl, setRepoUrl] = useState('')
  const [submittedRepo, setSubmittedRepo] = useState('')

  // React Query owns the fetch/loading/error/poll lifecycle. refetchInterval is
  // evaluated from the cached data, so a run that is already running on mount
  // resumes polling automatically (the old manual-interval version did not).
  const { data: runsData } = useQuery({
    queryKey: ['code-review-sage-runs'],
    queryFn: () => getJSON<{ runs: Run[] }>(`${API}/runs`),
    refetchInterval: (q) => (q.state.data?.runs?.[0]?.status === 'running' ? 3000 : false),
  })
  const run = runsData?.runs?.[0] ?? null

  const { data: settings } = useQuery({
    queryKey: ['code-review-sage-settings'],
    queryFn: () => getJSON<SettingsResp>(`${API}/settings`),
  })

  const reviewMut = useMutation({
    mutationFn: (links: string) => sendJSON(`${API}/review`, { links }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['code-review-sage-runs'] }),
  })

  // Repo mode: list a repo's open PRs (with reviewed/updated/new status), and
  // kick off a batch review of the un-reviewed ones (or ALL, when forced).
  const { data: repoPrs, isFetching: repoLoading, error: repoQueryErr, refetch: refetchRepo } = useQuery({
    queryKey: ['code-review-sage-repo-prs', submittedRepo],
    queryFn: () => getJSON<RepoPrsResp>(`${API}/repo-prs?repo=${encodeURIComponent(submittedRepo)}`),
    enabled: !!submittedRepo,
  })
  const reviewRepoMut = useMutation({
    mutationFn: (body: { repo: string; force?: boolean }) => sendJSON(`${API}/review-repo`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['code-review-sage-runs'] }),
  })

  // When a run finishes, refresh the repo PR list so just-reviewed PRs stop
  // showing as "new"/"updated" (the /repo-prs query is otherwise cached on
  // submittedRepo and wouldn't re-fetch on its own).
  useEffect(() => {
    if (submittedRepo && run && run.status !== 'running') {
      qc.invalidateQueries({ queryKey: ['code-review-sage-repo-prs', submittedRepo] })
    }
  }, [run?.status, run?.run_id, submittedRepo, qc])

  const saveMut = useMutation({
    mutationFn: (patch: Partial<Settings>) => sendJSON(`${API}/settings`, patch, 'PUT'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['code-review-sage-settings'] }),
  })

  // ── Self-learning: namespaces + the patterns/candidates each has learned ──
  const [nsInput, setNsInput] = useState('')
  const [openNs, setOpenNs] = useState<string | null>(null)
  const [showNsInput, setShowNsInput] = useState(false)

  const { data: nsData } = useQuery({
    queryKey: ['code-review-sage-namespaces'],
    queryFn: () => getJSON<NamespacesResp>(`${API}/namespaces`),
  })

  // Patterns/candidates for the expanded namespace (only fetched when one is open).
  const { data: learnings } = useQuery({
    queryKey: ['code-review-sage-learnings', openNs],
    queryFn: () => getJSON<LearningsResp>(`${API}/learnings?namespace=${encodeURIComponent(openNs || 'default')}`),
    enabled: !!openNs,
  })

  const invalidateNs = () => {
    qc.invalidateQueries({ queryKey: ['code-review-sage-namespaces'] })
    qc.invalidateQueries({ queryKey: ['code-review-sage-settings'] })
  }
  const createNsMut = useMutation({
    mutationFn: (name: string) => sendJSON(`${API}/namespaces`, { name }),
    onSuccess: () => { setNsInput(''); setShowNsInput(false); invalidateNs() },
  })
  const deleteNsMut = useMutation({
    mutationFn: (name: string) => sendJSON(`${API}/namespaces`, { name }, 'DELETE'),
    onSuccess: (_d, name) => { if (openNs === name) setOpenNs(null); invalidateNs() },
  })

  // Active namespaces are persisted in review settings; toggling one rewrites
  // the whole active list (the backend clamps it to at least ["default"]).
  const activeSet = new Set(settings?.settings?.active_namespaces ?? nsData?.active ?? ['default'])
  const toggleActive = (name: string, on: boolean) => {
    const next = on ? [...activeSet, name] : [...activeSet].filter(n => n !== name)
    saveMut.mutate({ active_namespaces: next.length ? next : ['default'] })
  }
  const nsErr = (createNsMut.error || deleteNsMut.error) instanceof Error
    ? (createNsMut.error || deleteNsMut.error as Error).message : ''

  const s = settings?.settings
  // Rows are keyed by the derived change_id (what the backend writes progress
  // under). Fall back to raw links only for legacy runs recorded before
  // change_ids existed. Pair each id with its raw link for the row href.
  const rows = (run?.change_ids ?? run?.changes ?? []).map((id, i) => ({
    id,
    link: run?.changes?.[i],
  }))
  const running = reviewMut.isPending || reviewRepoMut.isPending || run?.status === 'running'
  const reviewErr = reviewMut.error instanceof Error ? reviewMut.error.message : ''

  const startReview = () => {
    const links = input.trim()
    if (!links || running) return
    reviewMut.mutate(links)
  }

  const repoErr = (repoQueryErr instanceof Error ? repoQueryErr.message : '')
    || (reviewRepoMut.error instanceof Error ? reviewRepoMut.error.message : '')
  const unreviewedCount = (repoPrs?.prs ?? []).filter(p => !p.reviewed).length
  // The action buttons operate on the LISTED repo (submittedRepo), which can
  // differ from what's currently typed in the box.
  const staleList = !!submittedRepo && repoUrl.trim() !== submittedRepo
  const listRepo = () => {
    const u = repoUrl.trim()
    if (!u) return
    if (u === submittedRepo) refetchRepo()   // same URL -> force a real refresh
    else setSubmittedRepo(u)
  }
  const reviewRepo = (force: boolean) => {
    if (!submittedRepo || running) return
    if (force && !confirm(
      `Re-review ALL ${repoPrs?.count ?? 0} open PRs in ${submittedRepo}? `
      + 'This ignores the reviewed history and can be costly.')) return
    reviewRepoMut.mutate({ repo: submittedRepo, force })
  }

  return (
    <div className="p-6 max-w-[860px] mx-auto text-text">
      <h1 className="flex items-center gap-2.5 text-xl"><ScanSearch size={22} /> {i18nT('apps.codeReviewSage.codeReviewSagePage.code_review_sage')}</h1>
      <p className="text-muted text-[13px] mt-1">
        {i18nT('apps.codeReviewSage.codeReviewSagePage.self_evolving_deep_reviewer_for_github_prs_findi')}
      </p>

      {/* Always-visible GitHub one-time setup hint */}
      <div className="text-xs text-text bg-bg border border-border rounded-md px-3 py-2.5 my-3.5 leading-relaxed">
        <GitPullRequest size={14} className="inline align-middle mr-1.5" />
        <strong>{i18nT('apps.codeReviewSage.codeReviewSagePage.github_pr_one_time_setup')}</strong> {i18nT('apps.codeReviewSage.codeReviewSagePage.the_review_runs_on_the_gateway_host_and_needs_th')} <code>{i18nT('apps.codeReviewSage.codeReviewSagePage.gh')}</code> {i18nT('apps.codeReviewSage.codeReviewSagePage.cli_authenticated_there_run')}{' '}
        <code>{i18nT('apps.codeReviewSage.codeReviewSagePage.gh_auth_login_hostname_github_com')}</code> {i18nT('apps.codeReviewSage.codeReviewSagePage.once_never_paste_a_token_into_this_page_findings')} <em>{i18nT('apps.codeReviewSage.codeReviewSagePage.unavailable_could_not_be_fetched')}</em> {i18nT('apps.codeReviewSage.codeReviewSagePage.usually_means')}{' '}
        <code>{i18nT('apps.codeReviewSage.codeReviewSagePage.gh')}</code> {i18nT('apps.codeReviewSage.codeReviewSagePage.is_not_authenticated_on_the_gateway_host')}
      </div>

      {/* Repo mode — enumerate a repo's open PRs and review the un-reviewed ones */}
      <div className="border border-border rounded-md p-3 my-3.5">
        <div className="text-[13px] font-medium flex items-center gap-1.5 mb-2">
          <FolderGit2 size={14} /> {i18nT('apps.codeReviewSage.codeReviewSagePage.review_a_whole_repository')}
        </div>
        <div className="flex items-center gap-2">
          <input
            value={repoUrl}
            onChange={e => setRepoUrl(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') listRepo() }}
            aria-label={i18nT('apps.codeReviewSage.codeReviewSagePage.repository_url')}
            placeholder={i18nT('apps.codeReviewSage.codeReviewSagePage.https_github_com_owner_repo')}
            className="flex-1 box-border text-[13px] px-3 py-2 rounded-md bg-bg text-text border border-border font-mono"
          />
          <button onClick={listRepo} disabled={!repoUrl.trim() || repoLoading}
            className="inline-flex items-center gap-1 text-xs px-2.5 py-2 rounded-md border border-border text-muted hover:text-text hover:border-border-strong disabled:opacity-30 cursor-pointer bg-transparent">
            <RefreshCw size={12} className={repoLoading ? 'animate-spin' : ''} /> {i18nT('apps.codeReviewSage.codeReviewSagePage.list_open_prs')}
          </button>
        </div>
        {repoErr && <div className="text-danger text-xs mt-2">{repoErr}</div>}
        {repoLoading && !repoPrs && <div className="text-muted text-xs mt-2">{i18nT('apps.codeReviewSage.codeReviewSagePage.listing_open_prs')}</div>}
        {staleList && (
          <div className="text-warn text-[11px] mt-2">
            {i18nT('apps.codeReviewSage.codeReviewSagePage.showing')} <span className="font-mono">{submittedRepo}</span> {i18nT('apps.codeReviewSage.codeReviewSagePage.click_list_open_prs_to_load_the_url_above')}
          </div>
        )}
        {repoPrs && (repoPrs.count === 0 ? (
          <div className="text-muted text-xs mt-3">{i18nT('apps.codeReviewSage.codeReviewSagePage.no_open_prs_in')} {repoPrs.repo}.</div>
        ) : (
          <div className="mt-3">
            <div className="flex items-center gap-2.5 text-xs mb-2 flex-wrap">
              <span className="text-muted">
                {repoPrs.repo}: {i18nT('apps.codeReviewSage.codeReviewSagePage.open_pr', { count: repoPrs.count })}, {unreviewedCount} {i18nT('apps.codeReviewSage.codeReviewSagePage.not_yet_reviewed')}
              </span>
              <div className="ml-auto flex items-center gap-2">
                <SendBtn onClick={() => reviewRepo(false)} disabled={running || unreviewedCount === 0}>
                  {running ? i18nT('apps.codeReviewSage.codeReviewSagePage.running') : `Review ${unreviewedCount} new`}
                </SendBtn>
                <button onClick={() => reviewRepo(true)} disabled={running || repoPrs.count === 0}
                  title={i18nT('apps.codeReviewSage.codeReviewSagePage.re_review_every_open_pr_ignoring_the_reviewed_hi')}
                  className="text-xs px-2.5 py-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong disabled:opacity-30 cursor-pointer bg-transparent">
                  {i18nT('apps.codeReviewSage.codeReviewSagePage.force_review_all_count', { count: repoPrs.count })}
                </button>
              </div>
            </div>
            <ul className="list-none p-0 max-h-64 overflow-auto">
              {repoPrs.prs.map(pr => (
                <li key={pr.change_id} className="flex items-center gap-2.5 text-xs py-1.5 border-b border-border">
                  <a href={pr.url} target="_blank" rel="noreferrer" className="font-mono text-accent shrink-0">#{pr.number}</a>
                  <span className="truncate text-text" title={pr.title}>{pr.title}</span>
                  <span className="ml-auto shrink-0">
                    {pr.reviewed
                      ? <span className="text-accent text-[10px] border border-border rounded px-1.5 py-0.5">{i18nT('apps.codeReviewSage.codeReviewSagePage.reviewed')}</span>
                      : pr.reviewed_stale
                        ? <span className="text-warn text-[10px] border border-border rounded px-1.5 py-0.5">{i18nT('apps.codeReviewSage.codeReviewSagePage.updated')}</span>
                        : <span className="text-muted text-[10px] border border-border rounded px-1.5 py-0.5">{i18nT('apps.codeReviewSage.codeReviewSagePage.new')}</span>}
                  </span>
                </li>
              ))}
            </ul>
            <div className="text-[11px] text-muted mt-1.5">
              {i18nT('apps.codeReviewSage.codeReviewSagePage.new_never_reviewed_updated_reviewed_before_but_t')}
            </div>
          </div>
        ))}
      </div>

      <div className="text-[11px] text-muted mb-1.5">{i18nT('apps.codeReviewSage.codeReviewSagePage.or_paste_individual_pr_links')}</div>
      <textarea
        value={input}
        onChange={e => setInput(e.target.value)}
        rows={3}
        placeholder={i18nT('apps.codeReviewSage.codeReviewSagePage.paste_one_or_more_github_pr_links_one_per_line_o')}
        className="w-full box-border text-[13px] px-3 py-2.5 rounded-md bg-bg text-text border border-border resize-y font-body"
      />
      <div className="flex items-center gap-3 mt-3">
        <SendBtn onClick={startReview} disabled={running || !input.trim()}>
          {running ? i18nT('apps.codeReviewSage.codeReviewSagePage.running') : i18nT('apps.codeReviewSage.codeReviewSagePage.review')}
        </SendBtn>
        {s && (
          <span className="ml-auto text-[11px] text-muted">
            {i18nT('apps.codeReviewSage.codeReviewSagePage.model')} {s.model || 'default'} {i18nT('apps.codeReviewSage.codeReviewSagePage.effort')} {s.effort || 'default'} {i18nT('apps.codeReviewSage.codeReviewSagePage.concurrency')} {s.max_concurrent ?? 5}
          </span>
        )}
      </div>
      <div className="text-[11px] text-muted mt-2.5">
        {i18nT('apps.codeReviewSage.codeReviewSagePage.each_pr_is_reviewed_in_its_own_clean_pooled_work')}
      </div>
      {reviewErr && <div className="text-danger text-xs mt-2.5">{reviewErr}</div>}

      {/* Current / last run */}
      {run && (
        <div className="mt-[22px] border-t border-border pt-4">
          <div className="flex items-center gap-2.5 text-[13px]">
            <strong>{i18nT('apps.codeReviewSage.codeReviewSagePage.run')} {run.status === 'running' ? '(in progress)' : run.status}</strong>
            {run.report_slug && (
              <a href={`/artifacts/${run.report_slug}`}
                className="ml-auto flex items-center gap-1 text-accent text-xs">
                {i18nT('apps.codeReviewSage.codeReviewSagePage.open_focus_report')} <ExternalLink size={12} />
              </a>
            )}
          </div>
          {run.error && <div className="text-danger text-xs mt-1.5">{run.error}</div>}
          <ul className="list-none p-0 mt-2.5">
            {rows.map(({ id, link }) => {
              const p = run.progress?.[id]
              const phase = p?.phase ?? 'queued'
              const counts = p?.counts
              return (
                <li key={id}
                  className="flex items-center gap-2.5 text-xs py-1.5 border-b border-border">
                  {link
                    ? <a href={link} target="_blank" rel="noreferrer"
                        className="font-mono text-accent">{changeLabel(id)}</a>
                    : <span className="font-mono">{changeLabel(id)}</span>}
                  <span className={`ml-auto flex items-center gap-1 ${phase === 'failed' ? 'text-danger' : 'text-muted'}`}>
                    {phaseLabel(phase)}
                    {counts && phase === 'done' && (
                      <>
                        <Circle size={9} className="text-danger ml-1.5" fill="currentColor" />
                        {counts.red ?? 0}
                        <Circle size={9} className="text-warn ml-1" fill="currentColor" />
                        {counts.yellow ?? 0}
                      </>
                    )}
                    {p?.error ? ` — ${p.error}` : ''}
                  </span>
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {/* Settings — model + effort inherit the app default unless overridden */}
      {settings && (
        <details className="mt-[22px]">
          <summary className="cursor-pointer text-[13px] flex items-center gap-1.5">
            <Settings size={13} /> {i18nT('apps.codeReviewSage.codeReviewSagePage.configuration')}
          </summary>
          <div className="flex gap-[18px] mt-3 flex-wrap">
            <label className="text-xs text-muted">
              {i18nT('apps.codeReviewSage.codeReviewSagePage.model_2')}{' '}
              <select value={s?.model ?? ''} onChange={e => saveMut.mutate({ model: e.target.value || null })}
                className="text-xs px-2 py-1 rounded-md bg-bg text-text border border-border">
                <option value="">{i18nT('apps.codeReviewSage.codeReviewSagePage.default_agent_config')}</option>
                {settings.models.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <label className="text-xs text-muted">
              {i18nT('apps.codeReviewSage.codeReviewSagePage.effort_2')}{' '}
              <select value={s?.effort ?? ''} onChange={e => saveMut.mutate({ effort: e.target.value })}
                className="text-xs px-2 py-1 rounded-md bg-bg text-text border border-border">
                <option value="">{i18nT('apps.codeReviewSage.codeReviewSagePage.default_model_provider')}</option>
                {settings.efforts.map(ef => <option key={ef} value={ef}>{ef}</option>)}
              </select>
            </label>
            <label className="text-xs text-muted" title={i18nT('apps.codeReviewSage.codeReviewSagePage.max_prs_reviewed_at_once_on_the_shared_runtime')}>
              {i18nT('apps.codeReviewSage.codeReviewSagePage.concurrency_2')}{' '}
              <select value={s?.max_concurrent ?? 5}
                onChange={e => saveMut.mutate({ max_concurrent: Number(e.target.value) })}
                className="text-xs px-2 py-1 rounded-md bg-bg text-text border border-border">
                {Array.from({ length: settings.max_concurrent_max ?? 30 }, (_, i) => i + 1)
                  .map(n => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
          </div>
        </details>
      )}

      {/* Self-learning — the reviewer mines misses into forward-looking patterns,
          grouped by namespace. Reviews load patterns from the ACTIVE namespaces;
          pending candidates are staged during reviews and merged into the ruleset
          by the human-triggered `learn-from-sage` skill (an AI merge, not a blind
          overwrite), so this panel curates but never auto-consolidates. */}
      {nsData && (
        <details className="mt-[22px]" open>
          <summary className="cursor-pointer text-[13px] flex items-center gap-1.5">
            <Brain size={13} /> {i18nT('apps.codeReviewSage.codeReviewSagePage.self_learning')}
          </summary>
          <p className="text-[11px] text-muted mt-2 leading-relaxed">
            {i18nT('apps.codeReviewSage.codeReviewSagePage.reviews_load_learned_patterns_from_the')} <strong>{i18nT('apps.codeReviewSage.codeReviewSagePage.active')}</strong> {i18nT('apps.codeReviewSage.codeReviewSagePage.namespaces_new_learnings_accrue_as')} <em>{i18nT('apps.codeReviewSage.codeReviewSagePage.pending_candidates')}</em>{i18nT('apps.codeReviewSage.codeReviewSagePage.run_the')}{' '}
            <code>{i18nT('apps.codeReviewSage.codeReviewSagePage.learn_from_sage')}</code> {i18nT('apps.codeReviewSage.codeReviewSagePage.skill_to_consolidate_them_into_the_ruleset')}
          </p>

          {/* At-a-glance: which namespaces reviews actually load right now. */}
          <div className="text-[11px] mt-2.5">
            <span className="text-muted">{i18nT('apps.codeReviewSage.codeReviewSagePage.loaded_during_reviews')} </span>
            <span className="text-accent font-medium">{[...activeSet].sort().join(', ')}</span>
          </div>

          <ul className="list-none p-0 mt-3">
            {nsData.namespaces.map(ns => {
              const isOpen = openNs === ns.name
              const isActive = activeSet.has(ns.name)
              return (
                <li key={ns.name} className="border-b border-border">
                  <div className="flex items-center gap-2.5 text-xs py-2">
                    <input type="checkbox" checked={isActive}
                      aria-label={`Load namespace ${ns.name} during reviews`}
                      title={isActive ? i18nT('apps.codeReviewSage.codeReviewSagePage.active_loaded_during_reviews_uncheck_to_disable') : i18nT('apps.codeReviewSage.codeReviewSagePage.inactive_check_to_load_during_reviews')}
                      onChange={e => toggleActive(ns.name, e.target.checked)}
                      className="cursor-pointer" />
                    <button onClick={() => setOpenNs(isOpen ? null : ns.name)}
                      className={`font-mono bg-transparent border-none cursor-pointer p-0 hover:text-accent ${isActive ? 'text-accent font-medium' : 'text-muted'}`}>
                      {ns.name}
                    </button>
                    {isActive
                      ? <span className="text-accent text-[10px]">{i18nT('apps.codeReviewSage.codeReviewSagePage.active')}</span>
                      : <span className="text-muted text-[10px]">{i18nT('apps.codeReviewSage.codeReviewSagePage.inactive')}</span>}
                    <span className="ml-auto flex items-center gap-3 text-muted">
                      <span title={i18nT('apps.codeReviewSage.codeReviewSagePage.consolidated_patterns_loaded_during_reviews')}>{i18nT('apps.codeReviewSage.codeReviewSagePage.pattern', { count: ns.patterns })}</span>
                      {ns.candidate > 0 && (
                        <span className="text-warn" title={i18nT('apps.codeReviewSage.codeReviewSagePage.pending_candidates_awaiting_consolidation')}>{ns.candidate} {i18nT('apps.codeReviewSage.codeReviewSagePage.pending')}</span>
                      )}
                      {ns.name !== 'default' && (
                        <Clickable aria-label={`Delete namespace ${ns.name} and all its learnings`}
                          className="cursor-pointer hover:text-danger inline-flex"
                          onClick={() => { if (confirm(`Delete namespace "${ns.name}" and all its learnings?`)) deleteNsMut.mutate(ns.name) }}>
                          <Trash2 size={12} />
                        </Clickable>
                      )}
                    </span>
                  </div>

                  {isOpen && (
                    <div className="pb-3 pl-6">
                      {(learnings?.patterns?.length ?? 0) === 0 && (learnings?.candidate?.length ?? 0) === 0 && (
                        <div className="text-[11px] text-muted italic">{i18nT('apps.codeReviewSage.codeReviewSagePage.no_learnings_yet_patterns_appear_here_after_revi')}</div>
                      )}
                      {learnings?.patterns?.map(p => (
                        <div key={p.id} className="text-[11px] mb-2">
                          <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] mr-1.5 ${
                            p.impact === 'high' ? 'text-danger border border-danger' : 'text-muted border border-border'}`}>{p.impact}</span>
                          <strong className="text-text">{p.title}</strong>
                          <div className="text-muted mt-0.5">{p.guidance}</div>
                        </div>
                      ))}
                      {(learnings?.candidate?.length ?? 0) > 0 && (
                        <div className="mt-2 pt-2 border-t border-border">
                          <div className="text-[10px] text-warn uppercase tracking-wide mb-1">{i18nT('apps.codeReviewSage.codeReviewSagePage.pending_consolidation')}</div>
                          {learnings?.candidate?.map(c => (
                            <div key={c.id} className="text-[11px] mb-1.5 text-muted">
                              <strong className="text-text">{c.title}</strong> — {c.guidance}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>

          {/* Namespace creation stays out of the way until asked for. */}
          {showNsInput ? (
            <div className="flex items-center gap-2 mt-3">
              <input value={nsInput} autoFocus onChange={e => setNsInput(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter' && nsInput.trim()) createNsMut.mutate(nsInput.trim())
                  if (e.key === 'Escape') { setShowNsInput(false); setNsInput('') }
                }}
                placeholder={i18nT('apps.codeReviewSage.codeReviewSagePage.new_namespace')}
                className="text-xs px-2 py-1 rounded-md bg-bg text-text border border-border" />
              <button onClick={() => nsInput.trim() && createNsMut.mutate(nsInput.trim())}
                disabled={!nsInput.trim() || createNsMut.isPending}
                className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-border text-muted hover:text-text hover:border-border-strong disabled:opacity-30 cursor-pointer bg-transparent">
                <Plus size={12} /> {i18nT('apps.codeReviewSage.codeReviewSagePage.add')}
              </button>
              <button onClick={() => { setShowNsInput(false); setNsInput('') }}
                className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer">
                {i18nT('apps.codeReviewSage.codeReviewSagePage.cancel')}
              </button>
            </div>
          ) : (
            <button onClick={() => setShowNsInput(true)}
              className="inline-flex items-center gap-1 text-[11px] text-muted hover:text-accent bg-transparent border-none cursor-pointer mt-3 p-0">
              <Plus size={12} /> {i18nT('apps.codeReviewSage.codeReviewSagePage.new_namespace_2')}
            </button>
          )}
          {nsErr && <div className="text-danger text-xs mt-2">{nsErr}</div>}
        </details>
      )}
    </div>
  )
}
