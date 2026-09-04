/**
 * Create Folders From Project — scan a project tree, tick what you want, create it.
 *
 * The page is a thin surface over two host endpoints and holds no scanning or
 * creation logic of its own. Three properties are worth stating because they are
 * what make the flow safe rather than merely convenient:
 *
 *  - **A scan is read-only, so the preview is the confirmation step.** Nothing is
 *    created until the user presses the create button, and what is sent is
 *    exactly the set ticked at that moment.
 *  - **Server prose is rendered verbatim.** A refused root produces the same
 *    sentence that creating a folder by hand would have produced. Re-wording it
 *    here would make two surfaces disagree about one refusal, so the server's
 *    `error` text is displayed as-is and only the surrounding chrome is
 *    localized.
 *  - **A stale preview is recoverable, not fatal.** The server refuses a
 *    selection it no longer offers; that is the user's tree having changed under
 *    an open page, so it resolves to a re-scan prompt rather than an error.
 *
 *  - **The root is chosen with the same picker as every other project
 *    directory.** A scan root is a project directory, so it is picked from the
 *    shared `ProjectPicker` (recent + browse) rather than pasted. Reusing it
 *    rather than reimplementing means a directory reachable in the sidebar's
 *    folder settings is reachable here too, spelled the same way. Free typing
 *    stays available for a path that is faster to say than to browse to.
 *
 * Everything interactive is a native control (`input type=checkbox`, `button`,
 * `input type=text`), which is what makes the whole preview keyboard-operable
 * without any key handling of its own.
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { FolderPlus, FolderCheck, FolderOpen, AlertTriangle, RefreshCw, ChevronDown, ChevronRight } from 'lucide-react'
import { Card, CardTitle, Btn, SendBtn, Input, Badge, EmptyState, PageHeader } from '../../components/ui'
import ProjectPicker from '../../components/ProjectPicker'
import { i18nT } from '../../i18n/t'
import {
  scanProject,
  scaffoldProject,
  ScaffoldApiError,
  STATUS_EMPTY,
  CODE_SELECTION_STALE,
  type Candidate,
  type ScanResult,
  type ScaffoldResult,
} from './api'

/** A candidate found below another candidate, with the package it sits inside. */
interface NestedRow {
  row: Candidate
  parentPath: string
}

/** The preview's two lists: what hangs off the scan root, and what sits deeper. */
interface Split {
  top: Candidate[]
  nested: NestedRow[]
}

/** Ties the disclosure button to the list it reveals, for `aria-controls`. */
const NESTED_LIST_ID = 'scaffolder-nested-list'

/** Sort rank per tier: confident rows lead, offered rows follow. */
const TIER_RANK: Record<Candidate['tier'], number> = { auto: 0, offered: 1 }

/**
 * Order rows by confidence, keeping the server's path order inside each tier.
 *
 * The two orderings are complementary rather than competing: confidence picks
 * which block a row sits in, and the server's path order picks its position
 * within that block. A stable sort is what makes the second half true, so this
 * relies on `Array#sort` being stable rather than comparing paths as a
 * tiebreak — comparing them would impose a *string* order where the server
 * already supplied a tree order.
 *
 * Presentation only: the tier a row carries and the set that is ticked are both
 * the server's, and neither is touched here.
 */
function byConfidence(rows: Candidate[]): Candidate[] {
  return [...rows].sort((a, b) => TIER_RANK[a.tier] - TIER_RANK[b.tier])
}

/**
 * Split the scan into the rows the preview leads with and the ones it defers.
 *
 * A candidate hanging off the scan root is a top-level answer to "what is in this
 * project". A candidate found *inside* another candidate is a different kind of
 * claim: usually a build layout that keeps a manifest below the package root
 * (`Tests/src/package.json`), which is a directory the user almost never wants a
 * folder for. Presenting the two as peers is what made the preview misread —
 * when exactly one package contains a nested manifest, a section titled after
 * that package reads as if the package had been demoted out of the list it is in
 * fact still ticked at the top of.
 *
 * So the split is by depth, not by parent: there is one deferred bucket for the
 * whole scan rather than one section per containing package, and no heading ever
 * repeats a package name that already appears as a row.
 *
 * Every candidate lands in exactly one list, keyed off the candidate's own
 * `parent_path` — grouping is derived here, from the flat list that is the
 * single copy of each candidate, so a row can never be invisible yet still
 * selectable. Both lists are
 * then ordered by confidence with a stable sort, so the ticked rows lead and the
 * server's path order survives inside each tier.
 */
function splitCandidates(scan: ScanResult): Split {
  const top: Candidate[] = []
  const nested: NestedRow[] = []
  for (const row of scan.candidates) {
    if (row.parent_path === null || row.parent_path === scan.root) top.push(row)
    else nested.push({ row, parentPath: row.parent_path })
  }
  const nestedByPath = new Map(nested.map((n) => [n.row.path, n]))
  const nestedOrdered = byConfidence(nested.map((n) => n.row))
    .map((row) => nestedByPath.get(row.path))
    .filter((n): n is NestedRow => n !== undefined)
  return { top: byConfidence(top), nested: nestedOrdered }
}

/** The server's own default tick state, which already excludes existing folders. */
function defaultSelection(scan: ScanResult): Set<string> {
  return new Set(scan.candidates.filter((c) => c.selected).map((c) => c.path))
}

function TierBadge({ tier }: { tier: Candidate['tier'] }) {
  return tier === 'auto'
    ? <Badge variant="ok">{i18nT('apps.projectScaffolder.projectScaffolderPage.tier_confident')}</Badge>
    : <Badge variant="muted">{i18nT('apps.projectScaffolder.projectScaffolderPage.tier_offered')}</Badge>
}

function CandidateRow({ row, checked, onToggle, parentName }: {
  row: Candidate
  checked: boolean
  onToggle: (path: string, next: boolean) => void
  /** Basename of the candidate this row was found inside, for deferred rows only. */
  parentName?: string
}) {
  const alreadySetUp = i18nT('apps.projectScaffolder.projectScaffolderPage.already_set_up')
  return (
    <li className="flex items-start gap-2.5 py-1.5" data-testid="candidate-row">
      <input
        type="checkbox"
        className="mt-[3px] shrink-0 accent-[var(--accent)] cursor-pointer disabled:cursor-not-allowed"
        // An existing folder is reported but never re-created, so its row is
        // informational: disabling the box is what makes that unmissable rather
        // than leaving a tick the create step would silently ignore.
        disabled={row.existing}
        checked={checked}
        onChange={(e) => onToggle(row.path, e.target.checked)}
        // The visible name repeats across sibling directories, so the path is
        // what disambiguates one checkbox from another in a screen-reader list.
        aria-label={row.existing ? `${row.path} (${alreadySetUp})` : row.path}
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[13px] font-medium text-text-strong">{row.name}</span>
          <TierBadge tier={row.tier} />
          {parentName && (
            // Which package this was found inside. The checkbox's own label is the
            // full path, so this is sighted-user affordance rather than new
            // information for a screen reader.
            <span className="inline-flex items-center gap-1 text-[11.5px] text-muted" data-testid="nested-inside">
              <FolderOpen size={12} className="lucide-inline" />
              {i18nT('apps.projectScaffolder.projectScaffolderPage.inside_name', { name: parentName })}
            </span>
          )}
          {row.existing && (
            <span className="inline-flex items-center gap-1 text-[11.5px] text-muted" data-testid="already-set-up">
              <FolderCheck size={12} className="lucide-inline" />
              {alreadySetUp}
            </span>
          )}
        </div>
        <div className="text-[11.5px] text-muted font-mono break-all mt-0.5">{row.path}</div>
        {row.signals.length > 0 && (
          <div className="text-[11.5px] text-muted mt-0.5">
            <span className="font-semibold">
              {i18nT('apps.projectScaffolder.projectScaffolderPage.signals')}
            </span>{' '}
            <span>{row.signals.map(signalLabel).join(', ')}</span>
          </div>
        )}
      </div>
    </li>
  )
}

/** Last segment of an absolute directory path, on either separator. */
function baseName(path: string): string {
  const segments = path.split(/[/\\]+/).filter(Boolean)
  return segments[segments.length - 1] ?? path
}

/**
 * A detection signal as a short phrase. The scanner's tokens (`git`, `.kiro`,
 * `member`, `manifest:<file>`) are identifiers, not prose — rendered raw they
 * fail the row's promise of saying *why* a directory was picked. An unknown
 * token falls through unchanged so a new scanner signal degrades to its id
 * rather than disappearing.
 */
function signalLabel(signal: string): string {
  if (signal === 'git') return i18nT('apps.projectScaffolder.projectScaffolderPage.signal_git')
  if (signal === '.kiro') return i18nT('apps.projectScaffolder.projectScaffolderPage.signal_kiro')
  if (signal === 'member') return i18nT('apps.projectScaffolder.projectScaffolderPage.signal_member')
  if (signal.startsWith('manifest:')) {
    return i18nT('apps.projectScaffolder.projectScaffolderPage.signal_manifest', {
      name: signal.slice('manifest:'.length),
    })
  }
  return signal
}

/** Paths a bulk toggle may actually change: an existing folder is never created. */
function tickablePaths(rows: Candidate[]): string[] {
  return rows.filter((r) => !r.existing).map((r) => r.path)
}

function BulkButtons({ paths, onBulk }: { paths: string[]; onBulk: (paths: string[], next: boolean) => void }) {
  if (!paths.length) return null
  return (
    <span className="flex items-center gap-1.5">
      <Btn type="button" onClick={() => onBulk(paths, true)}>
        {i18nT('apps.projectScaffolder.projectScaffolderPage.select_all')}
      </Btn>
      <Btn type="button" onClick={() => onBulk(paths, false)}>
        {i18nT('apps.projectScaffolder.projectScaffolderPage.select_none')}
      </Btn>
    </span>
  )
}

/** The rows hanging off the scan root — the preview's answer to what this project holds. */
function RootList({ rows, selected, onToggle, onBulk }: {
  rows: Candidate[]
  selected: Set<string>
  onToggle: (path: string, next: boolean) => void
  onBulk: (paths: string[], next: boolean) => void
}) {
  return (
    <fieldset className="border-0 p-0 m-0 mb-4" data-testid="preview-group">
      <legend className="flex items-start gap-2 flex-wrap w-full mb-1">
        <span className="text-[11.5px] font-semibold text-muted">
          {i18nT('apps.projectScaffolder.projectScaffolderPage.directly_under_the_root')}
        </span>
        <BulkButtons paths={tickablePaths(rows)} onBulk={onBulk} />
      </legend>
      <ul className="list-none p-0 m-0 divide-y divide-border">
        {rows.map((row) => (
          <CandidateRow
            key={row.path}
            row={row}
            checked={selected.has(row.path)}
            onToggle={onToggle}
          />
        ))}
      </ul>
    </fieldset>
  )
}

/**
 * The deferred rows, behind one collapsed disclosure.
 *
 * Collapsed by default because these are speculative: a manifest below a package
 * root is usually a build artifact of that package rather than a project in its
 * own right, so the common answer is "none of these" and the list is noise in the
 * way of the decision the user came to make. It is a disclosure rather than a
 * filter because the server did offer them and a few are real.
 *
 * Two consequences are deliberate. The count is in the summary so a collapsed
 * section still says how much it is hiding, and a ticked deferred row is counted
 * there too — the create step acts on the ticked set whether or not it is on
 * screen, so the summary is the only place that can admit it. And the bulk
 * buttons appear only while expanded, so no control can silently change rows the
 * reader cannot see.
 */
function NestedSuggestions({ rows, open, onOpenChange, selected, onToggle, onBulk }: {
  rows: NestedRow[]
  open: boolean
  onOpenChange: (next: boolean) => void
  selected: Set<string>
  onToggle: (path: string, next: boolean) => void
  onBulk: (paths: string[], next: boolean) => void
}) {
  const selectedHere = rows.filter((n) => selected.has(n.row.path)).length
  return (
    <fieldset className="border-0 p-0 m-0 mb-4 border-t border-border pt-3" data-testid="nested-suggestions">
      <legend className="flex items-center gap-2 flex-wrap w-full mb-1">
        {/* A native button, so the disclosure is reachable by Tab and operable by
            Enter and Space without any key handling of its own. */}
        <button
          type="button"
          data-testid="nested-toggle"
          aria-expanded={open}
          aria-controls={open ? NESTED_LIST_ID : undefined}
          onClick={() => onOpenChange(!open)}
          className="inline-flex items-center gap-1 text-[11.5px] font-semibold text-muted hover:text-text rounded cursor-pointer bg-transparent border-0 p-0"
        >
          {open
            ? <ChevronDown size={13} className="lucide-inline shrink-0" />
            : <ChevronRight size={13} className="lucide-inline shrink-0" />}
          {i18nT('apps.projectScaffolder.projectScaffolderPage.n_possible_sub_folders_inside_packages_above', {
            n: rows.length,
          })}
        </button>
        {selectedHere > 0 && (
          <Badge variant="muted" data-testid="nested-selected">
            {i18nT('apps.projectScaffolder.projectScaffolderPage.n_selected_inside', { n: selectedHere })}
          </Badge>
        )}
      </legend>
      {/* Its own row rather than a sibling of the disclosure toggle: the toggle
          is itself a button, so sharing the legend put three actions in one
          horizontal group. Still rendered only while expanded, so no control
          can silently change rows the reader cannot see. */}
      {open && (
        <div className="mb-1" data-testid="nested-bulk-row">
          <BulkButtons paths={tickablePaths(rows.map((n) => n.row))} onBulk={onBulk} />
        </div>
      )}
      {open && (
        <ul id={NESTED_LIST_ID} className="list-none p-0 m-0 divide-y divide-border" data-testid="nested-list">
          {rows.map(({ row, parentPath }) => (
            <CandidateRow
              key={row.path}
              row={row}
              parentName={baseName(parentPath)}
              checked={selected.has(row.path)}
              onToggle={onToggle}
            />
          ))}
        </ul>
      )}
    </fieldset>
  )
}

function WarningList({ warnings }: { warnings: string[] }) {
  if (!warnings.length) return null
  return (
    <div className="bg-warn-subtle text-warn rounded-md px-3 py-2 mb-3" data-testid="scan-warnings">
      <div className="flex items-center gap-1.5 text-[12px] font-semibold">
        <AlertTriangle size={13} className="lucide-inline" />
        {i18nT('apps.projectScaffolder.projectScaffolderPage.warnings')}
      </div>
      <ul className="list-disc pl-5 m-0 mt-1 text-[11.5px]">
        {/* Server prose, rendered verbatim. */}
        {warnings.map((w) => <li key={w}>{w}</li>)}
      </ul>
    </div>
  )
}

function ResultsCard({ result }: { result: ScaffoldResult }) {
  return (
    <Card data-testid="scaffold-results">
      <CardTitle>{i18nT('apps.projectScaffolder.projectScaffolderPage.results')}</CardTitle>
      <div className="flex items-center gap-2 flex-wrap mb-3">
        <Badge variant="ok" data-testid="result-created">
          {i18nT('apps.projectScaffolder.projectScaffolderPage.n_created', { n: result.created.length })}
        </Badge>
        <Badge variant="muted" data-testid="result-skipped">
          {i18nT('apps.projectScaffolder.projectScaffolderPage.n_already_existed', { n: result.skipped_existing.length })}
        </Badge>
        {result.failed.length > 0 && (
          <Badge variant="err" data-testid="result-failed">
            {i18nT('apps.projectScaffolder.projectScaffolderPage.n_failed', { n: result.failed.length })}
          </Badge>
        )}
      </div>
      {result.created.length > 0 && (
        <ul className="list-none p-0 m-0 text-[11.5px] text-muted font-mono">
          {result.created.map((c) => <li key={c.path} className="break-all py-0.5">{c.path}</li>)}
        </ul>
      )}
      {result.failed.length > 0 && (
        <ul className="list-none p-0 m-0 mt-3" data-testid="failed-rows">
          {result.failed.map((f) => (
            <li key={f.path} className="py-1 border-t border-border first:border-t-0">
              <div className="text-[11.5px] font-mono text-text break-all">{f.path}</div>
              {/* The server's own refusal prose and its machine-readable id. */}
              <div className="text-[11.5px] text-danger">{f.error}</div>
              <div className="text-[11px] text-muted font-mono">{f.code}</div>
            </li>
          ))}
        </ul>
      )}
      <WarningList warnings={result.warnings} />
    </Card>
  )
}

export default function ProjectScaffolderPage() {
  const [root, setRoot] = useState('')
  const [scan, setScan] = useState<ScanResult | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [result, setResult] = useState<ScaffoldResult | null>(null)
  const [rootError, setRootError] = useState('')
  // Kept separate from rootError so each message renders where its own action is:
  // the root field owns scan/validation prose, the create button owns create prose.
  const [createError, setCreateError] = useState('')
  const [stale, setStale] = useState<string[] | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  // Owned by the page rather than the section so a fresh scan re-collapses it:
  // the deferred rows of the previous tree are not the deferred rows of this one.
  const [nestedOpen, setNestedOpen] = useState(false)
  const rootInputRef = useRef<HTMLInputElement>(null)
  const browseRef = useRef<HTMLButtonElement>(null)

  const split = useMemo(() => (scan ? splitCandidates(scan) : null), [scan])
  const selectedCount = selected.size

  const scanMut = useMutation({
    mutationFn: (path: string) => scanProject(path),
    // A new scan invalidates the previous preview, its selection, and any prior
    // outcome, so all three are cleared before the request rather than after:
    // leaving a stale preview on screen during the scan invites confirming it.
    onMutate: () => {
      setRootError('')
      setStale(null)
      setResult(null)
      setScan(null)
      setSelected(new Set())
      setNestedOpen(false)
    },
    onSuccess: (next) => {
      setScan(next)
      setSelected(defaultSelection(next))
    },
    onError: (err) => {
      // Every scan refusal is about the root field, so it renders inline against
      // that field rather than as a page-level banner.
      setRootError(err instanceof Error ? err.message : String(err))
      rootInputRef.current?.focus()
    },
  })

  const toggle = useCallback((path: string, next: boolean) => {
    setSelected((prev) => {
      const copy = new Set(prev)
      if (next) copy.add(path)
      else copy.delete(path)
      return copy
    })
  }, [])

  const bulk = useCallback((paths: string[], next: boolean) => {
    setSelected((prev) => {
      const copy = new Set(prev)
      for (const path of paths) {
        if (next) copy.add(path)
        else copy.delete(path)
      }
      return copy
    })
  }, [])

  const createMut = useMutation({
    mutationFn: ({ scanRoot, picked }: { scanRoot: string; picked: string[] }) =>
      scaffoldProject(scanRoot, picked),
    onMutate: () => {
      setStale(null)
      // Clear the previous attempt's message, so a retry never shows a stale
      // failure beside a button that is currently working.
      setCreateError('')
    },
    onSuccess: (r) => {
      setResult(r)
      setCreateError('')
    },
    onError: (err) => {
      if (err instanceof ScaffoldApiError && err.code === CODE_SELECTION_STALE) {
        // Not a failure of the request so much as of the preview: the tree moved
        // under an open page. Offer the one action that fixes it.
        setStale(err.unknown)
      } else {
        // Beside the create button, NOT in the root field's error slot: the user
        // clicked at the bottom of a preview that can be screens long, and a
        // message rendered up in the project-directory card is off-screen — the
        // button reads as dead. The root slot is also only ever cleared by a
        // scan, so a create failure left there outlives its own cause.
        setCreateError(err instanceof Error ? err.message : String(err))
      }
    },
  })

  // Thin dispatchers over the mutations: the guards (blank root, an in-flight
  // request) belong to the page, the request lifecycle to react-query.
  const busy: '' | 'scan' | 'create' = scanMut.isPending
    ? 'scan'
    : createMut.isPending
      ? 'create'
      : ''

  const runScan = (path: string) => {
    const trimmed = path.trim()
    if (!trimmed || busy) return
    scanMut.mutate(trimmed)
  }

  const create = () => {
    if (!scan || busy) return
    // Exactly the paths ticked right now. Order is the preview's own, and the
    // server re-derives the set from a fresh scan regardless.
    const picked = scan.candidates.filter((c) => selected.has(c.path)).map((c) => c.path)
    createMut.mutate({ scanRoot: scan.root, picked })
  }

  const isEmpty = scan !== null && scan.status === STATUS_EMPTY

  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={i18nT('apps.projectScaffolder.projectScaffolderPage.create_folders_from_project')}
        subtitle={i18nT('apps.projectScaffolder.projectScaffolderPage.scan_a_project_directory_then_create_a_sidebar')}
      />
      <div className="flex-1 overflow-y-auto px-2 pb-6 md:px-6">
        <Card>
          <CardTitle>
            <FolderPlus size={14} className="lucide-inline" />
            {i18nT('apps.projectScaffolder.projectScaffolderPage.project_directory')}
          </CardTitle>
          {/* A form so Enter in the field submits, which is the shortest keyboard
              path from a chosen path to seeing the preview. */}
          <form
            className="flex items-center gap-2"
            onSubmit={(e) => { e.preventDefault(); runScan(root) }}
          >
            <Input
              ref={rootInputRef}
              value={root}
              onChange={(e) => setRoot(e.target.value)}
              placeholder={i18nT('apps.projectScaffolder.projectScaffolderPage.absolute_path_to_a_project_directory')}
              aria-label={i18nT('apps.projectScaffolder.projectScaffolderPage.project_directory')}
              aria-invalid={rootError ? true : undefined}
              aria-describedby={rootError ? 'scaffolder-root-error' : undefined}
              spellCheck={false}
              autoComplete="off"
            />
            {/* type=button so it never submits the form it sits inside. */}
            <Btn
              type="button"
              ref={browseRef}
              data-testid="scaffolder-browse"
              onClick={() => setPickerOpen(true)}
            >
              <FolderOpen size={13} className="lucide-inline" />
              {i18nT('apps.projectScaffolder.projectScaffolderPage.browse')}
            </Btn>
            <SendBtn type="submit" disabled={!root.trim() || busy !== ''}>
              {busy === 'scan'
                ? i18nT('apps.projectScaffolder.projectScaffolderPage.scanning')
                : i18nT('apps.projectScaffolder.projectScaffolderPage.scan')}
            </SendBtn>
          </form>
          {rootError && (
            // Verbatim server prose: the same sentence manual folder creation gives.
            <div
              id="scaffolder-root-error"
              role="alert"
              className="text-[12px] text-danger mt-2"
              data-testid="root-error"
            >
              {rootError}
            </div>
          )}
          {/* Portals to the body and anchors to the Browse button. Reused rather
           *  than reimplemented so picking a scan root stays identical to picking
           *  any other project directory.
           *
           *  A pick fills the field and stops there — it does not scan. That
           *  mirrors the folder-settings picker, where a selection stages into the
           *  draft and a separate action commits it. The scan is read-only, so
           *  auto-running it would be harmless but would make one picker apply
           *  immediately and the other not; instead focus returns to the field, so
           *  the path is visible and editable and Enter scans it. */}
          {pickerOpen && (
            <ProjectPicker
              open={true}
              onOpenChange={(o) => { if (!o) setPickerOpen(false) }}
              anchorRef={browseRef}
              onSelect={(path) => {
                setRoot(path)
                setPickerOpen(false)
                rootInputRef.current?.focus()
              }}
            />
          )}
        </Card>

        {isEmpty && scan && (
          <Card>
            <WarningList warnings={scan.warnings} />
            <EmptyState
              icon={<FolderPlus />}
              title={i18nT('apps.projectScaffolder.projectScaffolderPage.no_sub_projects_found')}
              subtitle={i18nT('apps.projectScaffolder.projectScaffolderPage.nothing_under_this_directory_looked_like_a_proje')}
              testId="scan-empty"
              action={
                <SendBtn onClick={create} disabled={busy !== ''}>
                  {busy === 'create'
                    ? i18nT('apps.projectScaffolder.projectScaffolderPage.creating')
                    : i18nT('apps.projectScaffolder.projectScaffolderPage.create_the_root_folder_only')}
                </SendBtn>
              }
            />
            {createError && (
              // The empty state's create uses the same mutation as the preview's,
              // so a refusal must render here too — the preview card that carries
              // the other error element does not exist on this branch, and the
              // button silently returning to idle would leave the user unable to
              // tell whether a folder now exists.
              <div
                role="alert"
                className="text-[12px] text-danger mt-2 text-center"
                data-testid="create-error"
              >
                {createError}
              </div>
            )}
          </Card>
        )}

        {scan && !isEmpty && (
          <Card>
            <CardTitle>
              {i18nT('apps.projectScaffolder.projectScaffolderPage.preview')}
            </CardTitle>
            <WarningList warnings={scan.warnings} />
            <div className="flex items-center gap-2 flex-wrap mb-3 text-[12px] text-muted">
              <span className="font-mono break-all text-text">{scan.root}</span>
              {scan.root_existing && (
                <Badge variant="muted" data-testid="root-existing">
                  {i18nT('apps.projectScaffolder.projectScaffolderPage.already_set_up')}
                </Badge>
              )}
            </div>
            {split && (
              <>
                <RootList
                  rows={split.top}
                  selected={selected}
                  onToggle={toggle}
                  onBulk={bulk}
                />
                {split.nested.length > 0 && (
                  <NestedSuggestions
                    rows={split.nested}
                    open={nestedOpen}
                    onOpenChange={setNestedOpen}
                    selected={selected}
                    onToggle={toggle}
                    onBulk={bulk}
                  />
                )}
              </>
            )}
            {stale && (
              <div className="bg-warn-subtle text-warn rounded-md px-3 py-2 mb-3" data-testid="stale-selection">
                <div className="text-[12px]">
                  {i18nT('apps.projectScaffolder.projectScaffolderPage.this_preview_no_longer_matches_the_directory_on')}
                </div>
                {stale.length > 0 && (
                  <ul className="list-disc pl-5 m-0 mt-1 text-[11.5px] font-mono">
                    {stale.map((path) => <li key={path} className="break-all">{path}</li>)}
                  </ul>
                )}
                <Btn type="button" className="mt-2" onClick={() => runScan(scan.root)}>
                  <RefreshCw size={12} className="lucide-inline" />
                  {i18nT('apps.projectScaffolder.projectScaffolderPage.re_scan')}
                </Btn>
              </div>
            )}
            <div className="flex items-center justify-between gap-3 flex-wrap border-t border-border pt-3">
              <span className="text-[12px] text-muted" data-testid="selected-count">
                {i18nT('apps.projectScaffolder.projectScaffolderPage.n_selected', { n: selectedCount })}
              </span>
              <SendBtn onClick={create} disabled={busy !== ''}>
                {busy === 'create'
                  ? i18nT('apps.projectScaffolder.projectScaffolderPage.creating')
                  : i18nT('apps.projectScaffolder.projectScaffolderPage.create_folders')}
              </SendBtn>
            </div>
            {createError && (
              // Verbatim server prose, at the locus of the click that caused it.
              <div
                role="alert"
                className="text-[12px] text-danger mt-2 text-right"
                data-testid="create-error"
              >
                {createError}
              </div>
            )}
          </Card>
        )}

        {result && <ResultsCard result={result} />}
      </div>
    </div>
  )
}
