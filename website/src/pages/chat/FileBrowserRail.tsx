import { useState } from 'react'
import { useDebouncedValue } from '../../apps/file-explorer/hooks'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Files, Diff, Search, X, RefreshCw, FileText } from 'lucide-react'
import { api } from '../../api/client'
import { fileGrep, type FileGrepHit } from '../../api/fileGrep'
import ErrorNotice from '../../components/ErrorNotice'
import { EmptyState } from '../../components/ui'
import Clickable from '../../components/Clickable'
import { cn } from '../../lib/utils'
import { useColumnResize } from '../../hooks/useColumnResize'
import { PierreWorkspaceTree } from '../../pierre/tree'

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
 */
export type TreeState = 'no-dir' | 'error' | 'ready'

export function useTreeState(projectDir: string | null | undefined): TreeState {
  const q = useQuery({
    queryKey: ['project-tree', projectDir ?? ''],
    queryFn: () => api.projectTree(projectDir ?? ''),
    enabled: !!projectDir,
    retry: false,
    staleTime: 10_000,
  })
  if (!projectDir) return 'no-dir'
  return q.isError ? 'error' : 'ready'
}

export function useTreeAvailable(projectDir: string | null | undefined): boolean {
  return useTreeState(projectDir) === 'ready'
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
            className="flex flex-none items-center justify-center w-[26px] h-[26px] rounded-[7px] bg-bg-elevated border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-default"
            title={t('pages.chat.fileBrowserRail.refresh')}
            aria-label={t('pages.chat.fileBrowserRail.refresh')}
          >
            <RefreshCw size={12} className={refreshing ? 'animate-spin' : ''} />
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
