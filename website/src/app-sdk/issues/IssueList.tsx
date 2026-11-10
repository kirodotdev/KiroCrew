/**
 * A paged issue list over `useIssues`, with an optional filter bar.
 *
 * The filter bar edits a copy of `query` (state, assignee, labels, search text).
 * Every edit is a new query, so `useIssues` starts again from the first page. A
 * new `query` prop replaces the edits.
 */
import { useEffect, useRef, useState } from 'react'
import { User, UserCheck } from 'lucide-react'
import type { IssueSearchRequest, IssueSummary } from '../../types'
import ErrorNotice from '../../components/ErrorNotice'
import SimpleSelect from '../../components/SimpleSelect'
import { Skeleton } from '../../components/ui'
import { pullRequestErrorDetails } from '../../utils/pullRequestErrors'
import { i18nT } from '../../i18n/t'
import { AppApiPermissionError } from '../apiError'
import { ISSUES_PATH, useIssues } from './useIssues'
import IssueCard, { type IssueView } from './IssueCard'

export interface IssueListProps {
  query: IssueSearchRequest
  /** Defaults to `condensed`. */
  view?: IssueView
  /** Shows the filter bar. Ignored while `query.raw` is set: a raw query replaces the structured filters. */
  showFilters?: boolean
  onSelect?: (issue: IssueSummary) => void
  /** Replaces the default empty-state text. */
  emptyText?: string
}

/** How long a typed filter waits before it becomes the query. */
const TYPING_DEBOUNCE_MS = 300

type Filters = Pick<IssueSearchRequest, 'state' | 'assignee' | 'labels' | 'text'>

function filtersOf(query: IssueSearchRequest): Filters {
  return { state: query.state, assignee: query.assignee, labels: query.labels, text: query.text }
}

function splitLabels(value: string): string[] | undefined {
  const labels = value.split(',').map(s => s.trim()).filter(Boolean)
  return labels.length ? labels : undefined
}

const STATES = ['open', 'closed', 'all']

type FilterUpdate = (update: (prev: Filters) => Filters) => void

/**
 * One text box whose value becomes a filter once typing pauses. The box owns
 * what is typed, so committing a trimmed value never rewrites it mid-word.
 */
function useTypedFilter(initial: string, commit: (value: string) => void): [string, (v: string) => void, (v: string) => void] {
  const [typed, setTyped] = useState<{ value: string; dirty: boolean }>({ value: initial, dirty: false })
  const commitRef = useRef(commit)
  commitRef.current = commit
  useEffect(() => {
    if (!typed.dirty) return
    const t = setTimeout(() => commitRef.current(typed.value), TYPING_DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [typed])
  return [
    typed.value,
    value => setTyped({ value, dirty: true }),
    value => setTyped({ value, dirty: false }),
  ]
}

function FilterBar({ filters, onChange }: { filters: Filters; onChange: FilterUpdate }) {
  const [text, setText] = useTypedFilter(filters.text ?? '', v => onChange(prev => ({ ...prev, text: v.trim() || undefined })))
  const [assignee, setAssignee, showAssignee] = useTypedFilter(filters.assignee ?? '', v => onChange(prev => ({ ...prev, assignee: v.trim() || undefined })))
  const [labels, setLabels] = useTypedFilter((filters.labels ?? []).join(', '), v => onChange(prev => ({ ...prev, labels: splitLabels(v) })))
  const mine = filters.assignee === 'me'
  const input = 'h-7 min-w-0 rounded-md border border-border bg-bg px-2 text-[12px] text-text placeholder:text-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring'
  return (
    <div role="search" aria-label={i18nT('appSdk.issues.filters')} className="flex flex-wrap items-center gap-1.5">
      <input type="search" className={`${input} min-w-[8rem] flex-[2_1_8rem]`} value={text}
        aria-label={i18nT('appSdk.issues.filter_text')}
        placeholder={i18nT('appSdk.issues.filter_text_placeholder')} onChange={e => setText(e.target.value)} />
      <SimpleSelect
        aria-label={i18nT('appSdk.issues.filter_state')}
        className="h-7 w-auto"
        options={STATES}
        optionLabels={[i18nT('appSdk.issues.state_open'), i18nT('appSdk.issues.state_closed'), i18nT('appSdk.issues.state_all')]}
        value={filters.state ?? 'open'}
        onChange={v => { const state = v as Filters['state']; onChange(prev => ({ ...prev, state })) }}
      />
      <input className={`${input} flex-[1_1_6rem]`} value={assignee}
        aria-label={i18nT('appSdk.issues.filter_assignee')}
        placeholder={i18nT('appSdk.issues.filter_assignee_placeholder')} onChange={e => setAssignee(e.target.value)} />
      <button type="button" aria-pressed={mine}
        className={`inline-flex h-7 shrink-0 cursor-pointer items-center gap-1 rounded-full border px-2.5 text-[12px] font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring ${mine ? 'border-accent bg-accent-subtle text-text' : 'border-border bg-bg-elevated text-muted hover:text-text'}`}
        onClick={() => {
          const next = mine ? undefined : 'me'
          showAssignee(next ?? '')
          onChange(prev => ({ ...prev, assignee: next }))
        }}>
        {mine ? <UserCheck size={13} aria-hidden="true" /> : <User size={13} aria-hidden="true" />}
        {i18nT('appSdk.issues.assigned_to_me')}
      </button>
      <input className={`${input} flex-[1_1_6rem]`} value={labels}
        aria-label={i18nT('appSdk.issues.filter_labels')}
        placeholder={i18nT('appSdk.issues.filter_labels_placeholder')} onChange={e => setLabels(e.target.value)} />
    </div>
  )
}

function ListSkeleton({ view }: { view: IssueView }) {
  return (
    <div className="flex flex-col gap-2" data-testid="issue-list-loading" aria-busy="true" aria-label={i18nT('appSdk.issues.loading')}>
      {[0, 1, 2].map(i => view === 'expanded'
        ? <Skeleton key={i} className="h-28 w-full rounded-lg" />
        : <Skeleton key={i} className="h-7 w-full" />)}
    </div>
  )
}

export default function IssueList({ query, view = 'condensed', showFilters = false, onSelect, emptyText }: IssueListProps) {
  const queryKey = JSON.stringify(query)
  const [edited, setEdited] = useState(() => ({ forQuery: queryKey, filters: filtersOf(query) }))
  // A new query from the app replaces whatever the filter bar had edited. Reset
  // during render, so the first request after it already uses the new query.
  let filters = edited.filters
  if (edited.forQuery !== queryKey) {
    filters = filtersOf(query)
    setEdited({ forQuery: queryKey, filters })
  }
  const setFilters: FilterUpdate = update => setEdited(prev => ({ ...prev, filters: update(prev.filters) }))
  const effective: IssueSearchRequest = { ...query, ...filters }
  const { issues, isLoading, error, hasNextPage, fetchNextPage, isFetchingNextPage, refetch } = useIssues(effective)

  if (error instanceof AppApiPermissionError) {
    return (
      <ErrorNotice
        variant="inline"
        testId="app-issue-list-not-declared"
        message={i18nT('appSdk.issues.not_declared', { path: ISSUES_PATH })}
        askAgent
      />
    )
  }

  const failure = error ? pullRequestErrorDetails(error) : null
  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid="app-issue-list">
      {/* Keyed by the app's query so a new one resets what the boxes hold. */}
      {/* A raw query replaces the structured filters at the route, so the bar
          would show choices that change nothing. */}
      {showFilters && !query.raw && <FilterBar key={queryKey} filters={filters} onChange={setFilters} />}
      {isLoading ? <ListSkeleton view={view} /> : failure && issues.length === 0 ? (
        <div className="flex flex-col items-start gap-1.5">
          <ErrorNotice variant="inline" testId="app-issue-list-error" message={failure.message || i18nT('appSdk.issues.load_failed')} askAgent />
          {failure.loginCommand && <code className="text-[11px] text-text">{failure.loginCommand}</code>}
          <button type="button" onClick={refetch}
            className="h-7 cursor-pointer rounded-md border border-border bg-transparent px-2 text-[12px] text-muted hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring">
            {i18nT('appSdk.issues.retry')}
          </button>
        </div>
      ) : issues.length === 0 ? (
        <p className="m-0 py-4 text-center text-[12px] text-muted" data-testid="app-issue-list-empty">
          {emptyText ?? i18nT('appSdk.issues.empty')}
        </p>
      ) : (
        <>
          <ul className={`m-0 flex list-none flex-col p-0 ${view === 'expanded' ? 'gap-2' : 'gap-0.5'}`} aria-label={i18nT('appSdk.issues.list_label')}>
            {issues.map(issue => (
              <li key={issue.url || `${issue.provider}:${issue.key}`} className="min-w-0">
                <IssueCard issue={issue} view={view} onSelect={onSelect} />
              </li>
            ))}
          </ul>
          {failure && <ErrorNotice variant="inline" testId="app-issue-list-error" message={failure.message || i18nT('appSdk.issues.load_failed')} askAgent />}
          {hasNextPage && (
            <button type="button" onClick={fetchNextPage} disabled={isFetchingNextPage}
              className="h-8 cursor-pointer self-center rounded-md border border-border bg-transparent px-3 text-[12px] text-muted hover:text-text disabled:cursor-default disabled:opacity-60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring">
              {isFetchingNextPage ? i18nT('appSdk.issues.loading_more') : i18nT('appSdk.issues.load_more')}
            </button>
          )}
        </>
      )}
    </div>
  )
}
