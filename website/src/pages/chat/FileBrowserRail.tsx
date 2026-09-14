import { useState } from 'react'
import { useDebouncedValue } from '../../apps/file-explorer/hooks'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Files, Diff, Search, X, RefreshCw, FileText } from 'lucide-react'
import { api } from '../../api/client'
import { fileGrep, type FileGrepHit } from '../../api/fileGrep'
import ErrorNotice from '../../components/ErrorNotice'
import { findReport, parseErrorCode } from '../../utils/errorReport'
import { EmptyState } from '../../components/ui'
import Clickable from '../../components/Clickable'
import { searchErrorCause, type SearchErrorCause } from '../../lib/searchErrorCause'
import { cn } from '../../lib/utils'
import { useColumnResize } from '../../hooks/useColumnResize'
import { PierreWorkspaceTree } from '../../pierre/tree'
import { errMessage } from '../../utils/thunkError'

/** Rail width bounds; the grip clamps between them. */
const RAIL_MIN_W = 300
const RAIL_MAX_W = 520
const RAIL_W_KEY = 'mc-files-rail-w'

/** All/Changed mode for the current page session. Module-level (not
 *  persisted): in-place tab navigation remounts the rail — the tab id
 *  changes — and the mode must survive that, while a fresh page load still
 *  defaults to All files. */
let sessionChangedMode = false

/** Name/Content mode for the current page session, remembered exactly like
 *  `sessionChangedMode` and for the same reason. Not persisted: content search
 *  costs a walk of the project on every keystroke, so a page load starts on the
 *  cheap filename filter and the user opts in. */
let sessionSearchMode: SearchMode = 'name'

/** Which search the field runs: filter the tree by FILE NAME, or grep file
 *  CONTENTS (including inside Office documents) under the project root. */
type SearchMode = 'name' | 'content'

/** The backend's own floor — a shorter query returns nothing, so asking is a
 *  wasted round trip. Mirrors `_GREP_MIN_QUERY_CHARS` in `handlers/files.py`. */
const CONTENT_MIN_CHARS = 2

/** Keystroke debounce before a content search leaves the browser. A filename
 *  filter is local and instant; a content search is a bounded walk on the
 *  gateway, so it waits for the typing to settle. */
const CONTENT_DEBOUNCE_MS = 250

/** Filter text for the current page session, keyed by project directory.
 *  Module-level like `sessionChangedMode` (and deliberately NOT localStorage:
 *  a filter is session-scoped intent, and a stale filter surviving a page
 *  reload would hide the tree with no visible reason): in-place tab
 *  navigation remounts the rail and the typed filter must survive that. */
const sessionQuery = new Map<string, string>()

/** Cap on remembered project entries, mirroring the expansion memory's dir
 *  cap: delete-then-set keeps insertion order least-recently-written-first,
 *  so a long-lived tab drops the stalest project's filter, not the newest. */
const MAX_SESSION_QUERY_DIRS = 20
function rememberQuery(projectDir: string, value: string): void {
  sessionQuery.delete(projectDir)
  // An empty filter is indistinguishable from no entry: storing it would
  // occupy an LRU slot (evicting some other project's live filter) for
  // nothing, so clearing removes the entry outright.
  if (value === '') return
  sessionQuery.set(projectDir, value)
  for (const k of sessionQuery.keys()) {
    if (sessionQuery.size <= MAX_SESSION_QUERY_DIRS) break
    sessionQuery.delete(k)
  }
}

/** Whether the tree APIs answer for this directory. Shares the tree
 *  component's query key, so the probe costs no extra request. */
/**
 * Why the tree is or is not usable, which is NOT a boolean: a fetch that failed
 * and a chat with no project directory need different words and different
 * remedies. Collapsing them sends the user to fix a setting that is already
 * correct — the header is naming the directory while the body denies it exists.
 *
 * `ready` covers the in-flight case on purpose: the tree renders its own loading
 * state, so the rail should mount rather than flashing an error first.
 *
 * `error` stays ONE state because the rail renders the same notice either way, keyed on
 * the deadline-vs-other split the pickers use. Whether it counts as AVAILABLE is
 * cause-keyed, exactly as the pickers' Retry is: see `useTreeAvailable`.
 */
export type TreeState = 'no-dir' | 'error' | 'recoverable' | 'ready'

function useTreeQuery(projectDir: string | null | undefined) {
  return useQuery({
    queryKey: ['project-tree', projectDir ?? ''],
    queryFn: () => api.projectTree(projectDir ?? ''),
    enabled: !!projectDir,
    retry: false,
    staleTime: 10_000,
  })
}

/**
 * A cause, reconsidered against ONE endpoint's own codes.
 *
 * Only the degraded `failed` is reconsidered, because `failed` is retryable — so an endpoint whose
 * refusal the shared map has no entry for silently offered a Refresh that can only fail again. A
 * deadline stays `timed_out`, and a refusal the shared map does carry stays `denied`. Duck-typed on
 * `body`: the suites reject with an ApiError-shaped plain object against a mocked `api/client`.
 */
export function causeWithEndpointCodes(
  err: unknown,
  byCode: Record<string, SearchErrorCause>,
): SearchErrorCause {
  const cause = searchErrorCause(err)
  if (cause !== 'failed') return cause
  const body = typeof err === 'object' && err !== null ? (err as { body?: unknown }).body : undefined
  const code = parseErrorCode(typeof body === 'string' ? body : undefined)
  return (code && byCode[code]) || cause
}

/** The TREE endpoint spells an unreachable root `unknown_project_dir`; the shared map has no entry. */
const TREE_CAUSE_BY_CODE: Record<string, SearchErrorCause> = {
  unknown_project_dir: 'root_missing',
}

/**
 * The TREE notice copy, keyed by the same cause the listing arm uses.
 *
 * `denied` and `root_missing` borrow the listing arm's strings, exactly as `LISTING_FAILURE_KEYS`
 * does: a refusal is the same fact whichever read hit it. Collapsing them into the generic key left
 * a refused tree read saying only "Couldn't load the file tree", so its reason had to be inferred
 * from the absent remedy clause -- the same guess this change removes on the listing arm.
 */
const TREE_FAILURE_KEYS: Record<SearchErrorCause, string> = {
  timed_out: 'pages.chat.filesHome.tree_error',
  failed: 'pages.chat.filesHome.tree_error',
  denied: 'pages.chat.folderPanel.search_denied',
  root_missing: 'pages.chat.folderPanel.search_root_missing',
}

// Named only where re-asking can help: a refusal returns the same answer, so pointing a denied
// or missing notice at Refresh would offer a remedy that cannot work.
export const RETRYABLE_CAUSES: ReadonlySet<SearchErrorCause> = new Set(['timed_out', 'failed'])

export function useTreeState(projectDir: string | null | undefined): TreeState {
  const q = useTreeQuery(projectDir)
  if (!projectDir) return 'no-dir'
  if (!q.isError) return 'ready'
  // A deadline or a codeless failure: either can answer differently on a Refresh, so the rail
  // must stay to carry one. A refusal or a missing root cannot, and stays hidden.
  const cause = causeWithEndpointCodes(q.error, TREE_CAUSE_BY_CODE)
  return RETRYABLE_CAUSES.has(cause) ? 'recoverable' : 'error'
}

/**
 * The tree failure as one composed line, or null while the read has not failed.
 *
 * Every surface that can render a tree failure reads it from here, because the cause rule and the
 * remedy gate spelled per surface is how the surfaces came to disagree in the first place.
 */
export function useTreeNotice(
  projectDir: string | null | undefined,
  t: (key: string) => string,
): string | null {
  const q = useTreeQuery(projectDir)
  if (!projectDir || !q.isError) return null
  return failureMessage(t, TREE_FAILURE_KEYS, causeWithEndpointCodes(q.error, TREE_CAUSE_BY_CODE))
}

/**
 * Whether the rail is worth mounting. Delegates to `useTreeState` rather than re-spelling the
 * cause rule, because two spellings of one rule is how the surfaces came to disagree.
 *
 * A RECOVERABLE read keeps the rail — a deadline or a codeless failure, both of which can answer
 * differently on a Refresh. On the FILE-TAB rail hiding it strands the user outright: `SidePanel`
 * drops the rail AND its toggle with no notice, so there is no statement of the failure and no way
 * to re-ask. The Files-home surface is not that case — base already rendered a `tree_error` notice
 * beside a labelled Refresh — so there this is a regroup for one cause rule, not a rescue. A denial
 * or a missing root returns the same answer however often it is re-asked, so those keep the
 * hidden-rail behaviour rather than promising a recovery that cannot arrive.
 */
export function useTreeAvailable(projectDir: string | null | undefined): boolean {
  const state = useTreeState(projectDir)
  return state === 'ready' || state === 'recoverable'
}

/**
 * `cause — remedy`, joined in ONE place so no surface voices a failure differently.
 *
 * Callers gate it on the cause: a refusal answers the same however often it is re-asked, so
 * naming Refresh there would offer a remedy that cannot arrive.
 */
export function withRemedy(t: (key: string) => string, named: string): string {
  return `${named} — ${t('pages.chat.folderPanel.refresh_retries')}`
}

/**
 * The failure copy, naming the control that retries it.
 *
 * The recovery is an icon-only Refresh, so a notice that only states the cause leaves the remedy
 * undiscoverable until the user clicks an unrelated-looking button.
 */
export function failureMessage(
  t: (key: string) => string,
  keys: Record<SearchErrorCause, string>,
  cause: SearchErrorCause,
): string {
  const named = t(keys[cause])
  return RETRYABLE_CAUSES.has(cause) ? withRemedy(t, named) : named
}

/** A hit's path as the rail shows it: relative to the searched root, because the
 *  absolute prefix is the same on every row and is what pushes the informative
 *  tail out of a 300px rail. */
function shortenPath(path: string, root: string): string {
  if (root && path.startsWith(root)) return path.slice(root.length).replace(/^\//, '')
  return path
}

/**
 * The preview line with the matched run marked. The search is
 * case-insensitive, so the run is located on a folded copy and then sliced out
 * of the ORIGINAL — highlighting the folded text would render the file's own
 * casing wrong.
 */
function HighlightedPreview({ text, query }: { text: string; query: string }) {
  // Case-folded per CHARACTER, not with a whole-string toLowerCase: for some
  // characters lowercasing changes length (Turkish `\u0130` -> `i\u0307`), and an index
  // into the folded string then does not address the original, so the mark lands
  // on the wrong characters. Folding each character and keeping the ones whose
  // fold is a single character keeps both strings the same length, so one index
  // addresses both.
  const fold = (s: string) =>
    Array.from(s, c => {
      const lower = c.toLowerCase()
      return lower.length === 1 ? lower : c
    }).join('')
  const at = query ? fold(text).indexOf(fold(query)) : -1
  if (at < 0) return <>{text}</>
  return (
    <>
      {text.slice(0, at)}
      <mark className="bg-accent/25 text-text rounded-[2px] px-[1px]">
        {text.slice(at, at + query.length)}
      </mark>
      {text.slice(at + query.length)}
    </>
  )
}

/**
 * The content-search results list, styled after the Files app's `SearchPanel`:
 * one row per file, its root-relative path, `:line` for a text hit or the
 * location badge for a document hit, and a one-line preview with the match
 * marked.
 */
function ContentResults({ query, projectDir, onOpen }: {
  query: string
  projectDir: string
  onOpen: (hit: FileGrepHit) => void
}) {
  const { t } = useTranslation()
  const settled = useDebouncedValue(query, CONTENT_DEBOUNCE_MS).trim()
  const enabled = settled.length >= CONTENT_MIN_CHARS
  const { data, isFetching, error } = useQuery({
    queryKey: ['file-grep', projectDir, settled],
    queryFn: () => fileGrep(projectDir, settled),
    enabled: enabled && !!projectDir,
    retry: false,
    // The same query re-run on a re-mount is the same answer: the rail remounts
    // on tab navigation, and re-walking the project for a query already on
    // screen is the one cost this feature must not pay twice.
    staleTime: 30_000,
    // Refining a query changes the key, which would blank `data` and leave the
    // list empty under "Searching..." for the debounce plus up to the whole 2s
    // budget. Keeping the previous answer on screen means typing narrows a
    // visible list instead of clearing it and refilling it.
    //
    // Only while the ROOT is unchanged, though. The key carries `projectDir` too,
    // so keeping the previous answer across every key change also keeps it across
    // a PROJECT switch -- and each row opens by absolute path, so those rows stay
    // clickable and open files from the project the user has just left. Narrowing
    // a query is the case worth smoothing; changing project is a different
    // question whose old answer is not an approximation of the new one.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey?.[1] === projectDir ? previous : undefined,
  })

  if (!enabled) {
    return (
      <div className="px-2 py-3 text-[11.5px] text-muted">
        {t('pages.chat.fileBrowserRail.content_hint')}
      </div>
    )
  }

  const results = data?.results ?? []
  const notes: string[] = []
  if (data) {
    notes.push(t('components.discoverySearchBar.result', { count: results.length }))
    // Every engine returns at most ONE row per file -- `--max-count 1` for ripgrep,
    // a `break` after the first matching line in the python walk, one segment per
    // document. So "8 results" beside eight single-line rows leaves it open whether
    // a result is a file or one spot inside a file, and under the second reading
    // the list looks like it is hiding the other matches in each file. The count
    // key itself is shared with two other surfaces and registered as a plural, so
    // the unit is named here instead of relabelled there.
    if (results.length > 0) notes.push(t('pages.chat.fileBrowserRail.content_one_row_per_file'))
    if (data.truncated) notes.push(t('pages.chat.fileBrowserRail.content_capped'))
    if (data.skipped_docs > 0) {
      notes.push(t('pages.chat.fileBrowserRail.content_docs_skipped', { count: data.skipped_docs }))
    }
  }
  // The engine is diagnostic, and its NAME is not the diagnostic: "Searched with
  // python." reads as a claim that the search was narrowed to Python FILES --
  // that the list is deliberately incomplete. What a user can act on is that this
  // host took the slow path, so that is what the tooltip says, and only on the
  // slow path. The fast path is the expectation and needs no gloss.
  const why =
    data?.engine === 'python'
      ? t('pages.chat.fileBrowserRail.content_engine_slow')
      : undefined

  return (
    <div className="flex flex-col min-h-0 flex-1">
      {/* A search that FAILED is an error surfaced to the user, so it renders
          through ErrorNotice rather than as a red line in the status row: that
          is the one component that recovers the route, endpoint, HTTP status and
          backend code from the error journal and offers them to the agent. A
          refused root or an exhausted probe pool is not something the user can
          fix by retyping. Hand-off on -- a read failure has nothing to lose. */}
      {error && (
        <div className="px-2 pb-1.5 shrink-0">
          <ErrorNotice
            variant="inline"
            className="whitespace-normal"
            message={t('pages.chat.fileBrowserRail.content_failed')}
            // The human-readable line above is not the journal key, so the
            // report is looked up by the error's own message: without it the
            // hand-off carries a generic sentence and none of the endpoint,
            // status or backend code the agent needs.
            report={findReport(error instanceof Error ? error.message : undefined)}
            askAgent
            testId="file-grep-error"
          />
        </div>
      )}
      <div
        className="px-2 pb-1 text-[10.5px] text-muted shrink-0"
        data-testid="file-grep-status"
        title={why || undefined}
      >
        {isFetching ? t('pages.chat.fileBrowserRail.content_searching') : notes.join(' · ')}
      </div>
      {data?.truncated && (
        <div className="px-2 pb-1 text-[10.5px] text-muted shrink-0" data-testid="file-grep-partial-why">
          {t('pages.chat.fileBrowserRail.content_partial_why')}
        </div>
      )}
      {/* A `slide 7` or `Sheet1 row 12` badge reads as a jump target, and the
          click cannot honour it: a document opens at its start, because the
          viewer is an extracted-text preview with nowhere to scroll to. Said
          once, visibly, above the list, rather than per row or only on hover. */}
      {/* Gated on the HIT being positionless (line 0), not on it carrying a tag:
          a Word hit has no tag, and gating on the tag dropped this note for
          exactly the result that needs it most. */}
      {results.some(h => h.line === 0) && (
        <div className="px-2 pb-1 text-[10.5px] text-muted shrink-0" data-testid="file-grep-doc-note">
          {t('pages.chat.fileBrowserRail.content_doc_note')}
        </div>
      )}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {!isFetching && !error && results.length === 0 ? (
          <EmptyState icon={<Search size={20} />} title={t('pages.chat.fileBrowserRail.content_no_matches')} />
        ) : (
          results.map((hit, index) => (
            <Clickable
              key={`${hit.file}:${hit.line}:${hit.label ?? ''}:${index}`}
              className="block w-full text-left px-2 py-1 rounded-md hover:bg-bg-hover cursor-pointer"
              onClick={() => onOpen(hit)}
            >
              <div className="flex items-center gap-1 text-[11.5px] text-text truncate">
                <FileText size={11} className="shrink-0 opacity-60" />
                <span className="truncate">{shortenPath(hit.file, data?.root ?? projectDir)}</span>
                {/* A document has no line to jump to, so the row names the place
                    inside itself instead — "p 3", "slide 7", "Sheet1 row 12".
                    The note above the list says the click opens the document at
                    its start. */}
                {/* No tooltip. It restated the visible note above the list AND
                    the label this span already renders, and being gated on the
                    label it was absent exactly where a reader most wanted it --
                    a .docx hit, which has no location to name. One explanation,
                    always visible, beats a hover that is missing on the row that
                    needs it. */}
                <span className="shrink-0 text-muted tabular-nums">
                  {/* A document hit carries line 0, so the `:line` fallback would
                      render `:0` -- a line that does not exist. A hit with no
                      position shows no tag at all. */}
                  {hit.label ? hit.label : hit.line > 0 ? `:${hit.line}` : ''}
                </span>
              </div>
              <div className="text-[11px] text-muted truncate pl-[16px]">
                <HighlightedPreview text={hit.preview} query={settled} />
              </div>
            </Clickable>
          ))
        )}
      </div>
    </div>
  )
}

/**
 * The file-browser rail: resize grip + tree column, headed by ONE row — an
 * icons-only All/Changed segment (tooltips carry the labels, Changed shows a
 * live count) with an always-open search field filling the rest — and a
 * Name/Content toggle in words on the row beneath it.
 *
 * Name mode feeds the tree's search session (the tree's own built-in bar is
 * disabled). Content mode replaces the tree with grep results from
 * `/api/file-grep`, which searches file CONTENTS under the project root —
 * including the text inside Word, PowerPoint and Excel documents.
 *
 * Both tree modes render the SAME Pierre tree; Changed feeds it the git-status
 * path set and its opens land in diff mode (`onFileOpen`'s second argument).
 */
export default function FileBrowserRail({ projectDir, onFileOpen, onAddToContext, selectedPath }: {
  projectDir: string
  /** `opts.line` opens the file scrolled to that line — a content-search hit. */
  onFileOpen: (absPath: string, diff: boolean, opts?: { line?: number }) => void
  /** Right-click "Add to context" on a tree row: forwards the ABSOLUTE path
   *  and whether it is a file or a directory up to the composer host. */
  onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void
  /** Currently-open file, echoed as the tree selection. */
  selectedPath?: string | null
}) {
  const { t } = useTranslation()
  const [changedMode, _setChangedMode] = useState(() => sessionChangedMode)
  const setChangedMode = (v: boolean) => {
    sessionChangedMode = v
    _setChangedMode(v)
  }
  const [searchMode, _setSearchMode] = useState<SearchMode>(() => sessionSearchMode)
  const setSearchMode = (v: SearchMode) => {
    sessionSearchMode = v
    _setSearchMode(v)
  }
  const [query, _setQuery] = useState(() => sessionQuery.get(projectDir) ?? '')
  // Rehydrate on an in-place projectDir change (React's adjust-state-on-prop
  // pattern, synchronous before paint): `useState` reads the map only on the
  // first mount, and without this a new project would inherit — and then
  // store under its own key — the previous project's filter.
  const [queryDir, setQueryDir] = useState(projectDir)
  if (queryDir !== projectDir) {
    setQueryDir(projectDir)
    _setQuery(sessionQuery.get(projectDir) ?? '')
  }
  const setQuery = (v: string) => {
    rememberQuery(projectDir, v)
    _setQuery(v)
  }

  const { isError: treeError, error: treeErr } = useTreeQuery(projectDir)
  const treeRecoverable = useTreeState(projectDir) === 'recoverable'
  const treeNotice = useTreeNotice(projectDir, t)
  const { data: status, isError: statusError } = useQuery({
    queryKey: ['git-status', projectDir],
    queryFn: () => api.projectGitStatus(projectDir),
    enabled: !!projectDir,
    refetchInterval: 5_000,
    refetchOnWindowFocus: true,
  })
  const changedCount = status?.files?.length ?? 0

  // Both queries poll (10s tree / 5s status); this is the "I changed something
  // outside the app, show me now" escape hatch. `refetchQueries` (not
  // `invalidateQueries`) so `refreshing` tracks the actual network round trip
  // and the spinner reflects real work.
  const qc = useQueryClient()
  const [refreshing, setRefreshing] = useState(false)
  const refresh = async () => {
    setRefreshing(true)
    try {
      await Promise.all([
        qc.refetchQueries({ queryKey: ['project-tree', projectDir] }),
        qc.refetchQueries({ queryKey: ['git-status', projectDir] }),
        // Content results are cached for 30s, so the escape hatch has to reach
        // them too or a refresh would leave a stale hit list beside a fresh tree.
        qc.refetchQueries({ queryKey: ['file-grep', projectDir] }),
      ])
    } finally {
      setRefreshing(false)
    }
  }
  // Reserved while the notice names this control, and while a read is in flight: an icon alone
  // leaves the remedy unnamed on the surface whose Refresh a reader has least reason to find.
  const reserveRefreshLabel = refreshing || treeRecoverable

  // The grip sits on the rail's LEFT edge, so the hook negates the drag delta
  // (edge: 'left'): dragging left grows the rail. Clamping and the persisted
  // width key are unchanged from the hand-rolled block this replaces.
  const rail = useColumnResize(
    RAIL_W_KEY,
    () => {
      const v = parseInt(localStorage.getItem(RAIL_W_KEY) || '', 10)
      return Number.isFinite(v) ? Math.min(RAIL_MAX_W, Math.max(RAIL_MIN_W, v)) : RAIL_MIN_W
    },
    RAIL_MIN_W,
    RAIL_MAX_W,
    undefined,
    undefined,
    'left',
  )

  const segBtn = (on: boolean) =>
    cn('flex items-center justify-center gap-1.5 h-[22px] px-2 rounded-[5px] text-[11.5px] font-medium cursor-pointer border-none transition-colors',
       on ? 'bg-bg text-text shadow-[0_0_0_1px_var(--border)]' : 'bg-transparent text-muted hover:text-text')

  const contentMode = searchMode === 'content'

  return (
    <>
      <div
        {...rail.handleProps}
        role="separator"
        aria-orientation="vertical"
        aria-label={t('pages.chat.fileBrowserRail.resize')}
        className="w-1 shrink-0 cursor-col-resize bg-transparent hover:bg-accent/40 active:bg-accent/60 transition-colors"
        style={{ touchAction: 'none' }}
      />
      <div style={{ width: rail.width }} className="shrink-0 min-h-0 border-l border-border flex flex-col">
        <div className="flex items-center gap-1.5 px-2 h-[40px] shrink-0 border-b border-border">
          {/* All/Changed scopes the TREE, and Content mode has no tree. Left
              rendered it kept its "Changed" highlight while the content results
              ignore it, so the rail would claim a scope it does not apply and the
              button would do nothing when clicked. Honouring it would be searching the
              staged-changes list, which is out of scope; disabling it would keep
              the misleading highlight. It comes back with the tree. */}
          {!contentMode && (
          <div
            className="flex flex-none bg-bg-elevated border border-border rounded-[7px] p-[2px] gap-[2px]"
            role="group"
            aria-label={t('pages.chat.fileBrowserRail.tree_mode')}
          >
            <button
              onClick={() => setChangedMode(false)}
              aria-pressed={!changedMode}
              className={segBtn(!changedMode)}
              title={t('pages.chat.fileBrowserRail.all_files')}
              aria-label={t('pages.chat.fileBrowserRail.all_files')}
            >
              <Files size={12} className="shrink-0" />
            </button>
            <button
              onClick={() => setChangedMode(true)}
              aria-pressed={changedMode}
              className={segBtn(changedMode)}
              title={t('pages.chat.fileBrowserRail.changed')}
              aria-label={t('pages.chat.fileBrowserRail.changed')}
            >
              <Diff size={12} className="shrink-0" />
              {changedCount > 0 && <span className="opacity-60 text-[10px] tabular-nums">{changedCount}</span>}
            </button>
          </div>
          )}
          <div className="flex flex-1 min-w-0 items-center gap-1.5 h-[26px] px-2 bg-bg-elevated border border-border focus-within:border-accent rounded-[7px] transition-colors">
            <Search size={12} className="text-muted shrink-0" />
            <input
              value={query}
              onChange={e => setQuery(e.target.value)}
              onKeyDown={e => { if (e.key === 'Escape') setQuery('') }}
              placeholder={contentMode
                ? t('pages.chat.fileBrowserRail.content_placeholder')
                : t('pages.chat.fileBrowserRail.filter_placeholder')}
              aria-label={contentMode
                ? t('pages.chat.fileBrowserRail.content_placeholder')
                : t('pages.chat.fileBrowserRail.filter_placeholder')}
              className="flex-1 min-w-0 bg-transparent border-none outline-none text-[12px] text-text"
            />
            {query && (
              <button
                onClick={() => setQuery('')}
                className="flex items-center justify-center w-[18px] h-[18px] rounded cursor-pointer text-muted hover:text-text bg-transparent border-none shrink-0"
                aria-label={t('pages.chat.fileBrowserRail.close_search')}
              >
                <X size={11} />
              </button>
            )}
          </div>
          <button
            onClick={refresh}
            disabled={refreshing}
            className={`flex flex-none items-center justify-center h-[26px] rounded-[7px] bg-bg-elevated border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-default ${
              reserveRefreshLabel ? 'gap-1 px-1.5' : 'w-[26px]'
            }`}
            title={t('pages.chat.fileBrowserRail.refresh')}
            aria-label={t('pages.chat.fileBrowserRail.refresh')}
          >
            <RefreshCw size={12} className={refreshing ? 'animate-spin' : ''} />
            {/* Laid out before it is needed, never animated: a 26px→auto morph cannot transition,
                so the growth is spent while the read is still in flight. */}
            {reserveRefreshLabel && (
              <span
                aria-hidden
                className={`text-[11px] leading-none ${treeRecoverable ? '' : 'invisible'}`}
              >
                {t('pages.chat.fileBrowserRail.refresh')}
              </span>
            )}
          </button>
        </div>
        {/* The Name/Content toggle has its own row, in words, both always
            showing. Words rather than icons because an icon pair is misread --
            readers take the inactive one for the active one -- and inside the
            field the pair eats its width down to ~8 visible characters of the
            user's own query. Two plain words on their own row
            cost 26px of height and remove both problems. The cost of a wrong
            read here is silent -- the same query becomes a different search --
            which is why this control gets words where All/Changed gets icons. */}
        <div className="flex items-center px-2 pt-1.5 shrink-0">
          <div
            className="flex w-full bg-bg-elevated border border-border rounded-[7px] p-[2px] gap-[2px]"
            role="group"
            aria-label={t('pages.chat.fileBrowserRail.search_mode')}
          >
            <button
              onClick={() => setSearchMode('name')}
              aria-pressed={!contentMode}
              className={cn(segBtn(!contentMode), 'flex-1')}
              title={t('pages.chat.fileBrowserRail.search_names')}
              aria-label={t('pages.chat.fileBrowserRail.search_names')}
            >
              {t('pages.chat.fileBrowserRail.mode_name')}
            </button>
            <button
              onClick={() => setSearchMode('content')}
              aria-pressed={contentMode}
              className={cn(segBtn(contentMode), 'flex-1')}
              title={t('pages.chat.fileBrowserRail.search_contents')}
              aria-label={t('pages.chat.fileBrowserRail.search_contents')}
            >
              {t('pages.chat.fileBrowserRail.mode_content')}
            </button>
          </div>
        </div>
        {/* A failed status read is surfaced, not swallowed: without this the
            Changed count silently reads 0, which is indistinguishable from a
            clean tree. Its own row under the header (the 40px header is full).
            File rail, no draft → hand-off on. */}
        {statusError && (
          <div className="px-2 pt-1.5 shrink-0">
            <ErrorNotice variant="inline" message={t('pages.chat.fileBrowserRail.git_status_failed')} askAgent />
          </div>
        )}
        {/* A bounded tree read that rejects used to paint an empty tree, which reads as
            an empty project. File rail, no draft -> hand-off on. */}
        {treeError && (
          <div className="px-2 pt-1.5 shrink-0 flex items-center gap-2">
            <ErrorNotice
              variant="inline"
              message={treeNotice ?? ''}
              report={findReport(errMessage(treeErr))}
              askAgent
            />
          </div>
        )}
        <div className="flex-1 min-h-0 flex flex-col py-1.5 pl-1">
          {contentMode ? (
            <ContentResults
              query={query}
              projectDir={projectDir}
              // A text hit carries the line it matched on. A document hit has no
              // navigable position (line 0), and passing 0 would ask for a line
              // that does not exist, so the file simply opens at its start.
              onOpen={hit => {
                const line = hit.line > 0 ? hit.line : undefined
                onFileOpen(hit.file, false, line !== undefined ? { line } : undefined)
              }}
            />
          ) : (
            <PierreWorkspaceTree
              mode={changedMode ? 'changed' : 'all'}
              projectDir={projectDir}
              persistExpansion
              onFileOpen={(abs) => onFileOpen(abs, changedMode)}
              onAddToContext={onAddToContext}
              searchQuery={query || null}
              selectedPath={selectedPath ?? null}
            />
          )}
        </div>
      </div>
    </>
  )
}
