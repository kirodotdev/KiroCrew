import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, Command, Cog, Loader2, MessageSquare, Package, Terminal } from 'lucide-react'

import { api } from '../../api/client'
import { appNavTargets } from '../../appNav'
import { useAppDispatch } from '../../store'
import { createSlot } from '../../store/chatSlice'
import { Highlighted } from '../../components/commandPalette/Highlighted'
import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import { settingsRoute } from '../../components/commandPalette/settingsRoute'
import { settingsSubtitle } from '../../components/commandPalette/settingsTabLabel'
import { usePaletteActions } from '../../components/commandPalette/paletteActions'
import { useSessionsProvider } from '../../components/commandPalette/providers/sessionsProvider'
import type { Result } from '../../components/commandPalette/types'
import { useVisualViewport } from '../../hooks/useVisualViewport'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'
import { useTheme } from '../../hooks/useTheme'
import { i18nT } from '../../i18n/t'
import { useLanguage } from '../../i18n/LanguageProvider'

import { loadUsage, recordUse, type UsageMap } from './frecency'
import { rankRootRows, type RankedRow, type RootGroup, type RootRow, type RootRowKind } from './rootIndex'

/**
 * Command Bar — the ⌘K launcher.
 *
 * Contributed by the `command-bar` app as an overlay claiming the host's
 * `quick-search` slot, so the host renders it only while that app is enabled.
 *
 * The shape it is built around: the FIRST PAGE is a launcher over rows already in
 * memory (commands, app destinations, quicklinks, settings) and never queries a
 * backend, so typing in it costs nothing no matter how much history the instance
 * holds. A content search is a ROW you enter — entering it is the activation
 * event that lets that engine run its first query. The previous surface fanned
 * every keystroke out to every provider, which is why typing could stall the
 * gateway's event loop; here a keystroke in the root has nothing to fan out to.
 *
 * No prefix sigils: the entry gesture is Enter on a row, and habit (frecency
 * ranking) is what makes a frequent row reachable in one or two keystrokes.
 */

/** Scoped views the bar can enter. Each one owns its own engine. */
type Scope = null | 'sessions'

const SESSIONS_MIN_CHARS = 2
const DEBOUNCE_MS = 150

function groupLabel(group: RootGroup): string {
  switch (group) {
    case 'commands':
      return i18nT('apps.commandBar.group_commands')
    case 'apps':
      return i18nT('apps.commandBar.group_apps')
    case 'settings':
      return i18nT('apps.commandBar.group_settings')
  }
}

/**
 * The row's own type, for the right-aligned label.
 *
 * Keyed off `kind` first: a `view` row opens a surface inside the bar, which is a
 * different promise from a row that acts and closes, and that difference matters
 * more to the reader than which group it was filed under. Everything else is named
 * by its group. There is deliberately no "Quicklink" case -- that group was removed
 * from this surface for having no writer, so a label for it would name a row type
 * the bar cannot produce.
 */
function kindLabel(row: { kind: RootRowKind; group: RootGroup }): string {
  if (row.kind === 'view') return i18nT('apps.commandBar.kind.view')
  if (row.group === 'apps') return i18nT('apps.commandBar.kind.app')
  if (row.group === 'settings') return i18nT('apps.commandBar.kind.setting')
  return i18nT('apps.commandBar.kind.command')
}

function groupIcon(group: RootGroup) {
  switch (group) {
    case 'commands':
      return <Terminal size={14} className="lucide-inline" />
    case 'apps':
      return <Package size={14} className="lucide-inline" />
    case 'settings':
      return <Cog size={14} className="lucide-inline" />
  }
}

export default function CommandBarOverlay({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const vv = useVisualViewport()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [scope, setScope] = useState<Scope>(null)
  const [selected, setSelected] = useState(0)
  const [usage, setUsage] = useState<UsageMap>(() => loadUsage())
  const [actionError, setActionError] = useState<string | null>(null)
  /** Row id whose `invoke` work is still resolving, or null. */
  const [pendingRow, setPendingRow] = useState<string | null>(null)

  const { navigate } = usePaletteActions()
  const { resolved } = useLanguage()
  const { cycle: cycleTheme } = useTheme()
  const dispatch = useAppDispatch()
  // `aria-modal` is a promise that Tab cannot reach the page behind the dialog, so
  // the trap has to be real. Escape is left to the input's own handler, which needs
  // it to pop a scope before it closes the bar.
  useDialogFocusTrap(dialogRef, onClose, true, false)
  // Constructing the sessions engine is just memoized closures — it issues no
  // request until `search()` is called, and only the sessions VIEW calls it. That
  // call site, not the construction, is what the root must never reach.
  // Constructed here but INERT until the sessions view is entered: the hook's own
  // ['instances'] query would otherwise fire on a warm install the moment the root
  // opened, which is exactly the request this surface promises not to make.
  const sessions = useSessionsProvider({ active: scope === 'sessions' })

  // The app list is READ, never fetched: the shell publishes its own
  // `GET /api/apps` response under this key, and `enabled: false` makes this a
  // cache subscriber that re-renders when that write lands. Fetching here would
  // reintroduce a request on any open past the stale window, which is exactly the
  // cost this surface exists to remove. Before the shell's first response the Apps
  // group is simply empty; commands and settings are local and render regardless.
  const { data: apps } = useQuery({
    queryKey: ['apps'],
    queryFn: () => api.listApps(),
    enabled: false,
  })

  useEffect(() => {
    if (!open) return
    setQuery('')
    setDebounced('')
    setScope(null)
    setSelected(0)
    setUsage(loadUsage())
    setActionError(null)
    setPendingRow(null)
  }, [open])

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query), DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [query])

  // A failure describes the row the user just activated, so it must not outlive the
  // query that produced it. The in-flight guard is deliberately NOT cleared here:
  // typing while work is resolving must not re-arm a second activation of it.
  useEffect(() => {
    setActionError(null)
  }, [query])

  const rootRows: RootRow[] = useMemo(() => {
    const rows: RootRow[] = [
      {
        id: 'command:new-session',
        title: i18nT('apps.commandBar.cmd_new_session'),
        group: 'commands',
        kind: 'invoke',
        // `dispatch(...).unwrap()` already returns a promise that rejects on failure,
        // which is the whole contract an `invoke` row needs. Wrapping it in a mutation
        // added state nothing reads, changed identity every render (so this memo never
        // held), and refused to re-run after a rejection -- leaving a failed New
        // Session unretryable without closing the bar.
        //
        // The navigate is part of the action, not decoration: created off-screen from
        // Settings or Task Runner the new session is invisible, so a success reads as a
        // failure and the user runs it again into a duplicate. The palette carried this
        // in the mutation's `onSuccess`; it belongs to the row either way.
        run: async () => {
          await dispatch(createSlot(undefined)).unwrap()
          navigate('/chat')
        },
        keywords: ['chat', 'start', 'blank'],
      },
      {
        id: 'command:toggle-theme',
        title: i18nT('apps.commandBar.cmd_toggle_theme'),
        // The cycle has three stops, so a hop onto `system` that happens to match the
        // current look changes nothing visible and reads as a silent failure. Naming
        // the cycle is what makes that outcome legible. Key already in the catalog.
        subtitle: i18nT('components.commandPalette.providers.actionsProvider.cycle_light_dark_system'),
        group: 'commands',
        kind: 'invoke',
        // Same side effect the palette's actions provider invokes, reached through the
        // theme context directly so the row needs nothing threaded into the overlay.
        run: async () => cycleTheme(),
        keywords: ['dark', 'light', 'appearance', 'colour', 'color'],
      },
      {
        id: 'command:search-sessions',
        title: i18nT('apps.commandBar.cmd_search_sessions'),
        group: 'commands',
        kind: 'view',
        view: 'sessions',
        keywords: ['history', 'chat', 'conversation'],
      },
    ]
    for (const target of appNavTargets(apps ?? [])) {
      rows.push({
        id: `app:${target.name}`,
        title: target.label,
        group: 'apps',
        kind: 'navigate',
        route: target.route,
      })
    }
    for (const entry of SETTINGS_REGISTRY) {
      rows.push({
        id: `setting:${entry.id}`,
        title: entry.labelKey ? i18nT(entry.labelKey) : entry.label,
        subtitle: settingsSubtitle(entry),
        group: 'settings',
        kind: 'navigate',
        route: settingsRoute(entry),
      })
    }
    return rows
    // `resolved` appears in the deps without appearing in the body on purpose: every
    // title and subtitle above is a catalog lookup, and a language change re-renders
    // the tree without remounting it, which does not recompute a memo. Omitting it
    // would freeze these rows in whichever language the surface first resolved.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apps, cycleTheme, dispatch, navigate, resolved])

  // The root ranks from the LIVE query, not the debounced one. Ranking is pure and
  // local, so there is nothing to throttle, and debouncing it would let a fast Enter
  // -- two keystrokes then return, which is the whole point of a launcher -- activate
  // the row selected against the previous query. Debounce exists for the scoped
  // views, which do hit the network.
  const ranked: RankedRow[] = useMemo(
    () => (scope ? [] : rankRootRows(rootRows, query, usage)),
    [scope, rootRows, query, usage],
  )

  // Sessions view. Enabled only inside the scope, so the root cannot trigger it.
  const scopedQuery = scope === 'sessions' ? debounced.trim() : ''
  const { data: scopedResults, isFetching, isError, refetch: refetchSessions } = useQuery({
    queryKey: ['command-bar', 'sessions', scopedQuery],
    queryFn: () => Promise.resolve(sessions.search(scopedQuery)) as Promise<Result[]>,
    enabled: scope === 'sessions' && scopedQuery.length >= SESSIONS_MIN_CHARS,
    staleTime: 15_000,
  })

  const use = useCallback((id: string) => setUsage(prev => recordUse(id, Date.now(), prev)), [])

  const enterScope = useCallback((view: Scope, keepQuery: string) => {
    setScope(view)
    setQuery(keepQuery)
    setDebounced(keepQuery)
    setSelected(0)
    inputRef.current?.focus()
  }, [])

  const activateRoot = useCallback(
    (row: RankedRow) => {
      // A second Enter while the first activation is still resolving would run the
      // work twice -- two sessions from one intent -- because the bar stays open
      // until the promise settles.
      if (pendingRow) return
      use(row.id)
      if (row.kind === 'view') {
        // Entering is the activation event: the engine's first query happens
        // here, not while the user was still typing in the root.
        enterScope((row.view as Scope) ?? null, '')
        return
      }
      if (row.kind === 'navigate' && row.route) {
        navigate(row.route)
        onClose()
        return
      }
      // An `invoke` row may do work that fails. Closing first would tell the user
      // it succeeded -- a new session that was never created looks identical to a
      // created one once the bar is gone -- so the bar closes only after the work
      // resolves, and a rejection keeps it open carrying the error.
      const pending = row.run?.()
      if (pending) {
        setPendingRow(row.id)
        void pending.then(
          () => {
            setPendingRow(null)
            onClose()
          },
          () => {
            setPendingRow(null)
            // Name the row and the way out: the bar deliberately stays open so Enter
            // retries, but that is invisible unless the copy says so.
            setActionError(i18nT('apps.commandBar.action_failed', { action: row.title }))
          },
        )
        return
      }
      onClose()
    },
    [enterScope, navigate, onClose, pendingRow, use],
  )

  const rows: (RankedRow | Result)[] = useMemo(
    () => (scope ? (scopedResults ?? []) : ranked),
    [scope, scopedResults, ranked],
  )
  const fallbackVisible = !scope && query.trim().length > 0
  // The recovery row exists for the dead end -- a typed query that matched nothing --
  // not for every keystroke. Riding `fallbackVisible` put a row about switching the
  // feature off under every successful search, and ArrowUp from the top wrapped
  // selection straight onto it.
  const recoveryVisible = fallbackVisible && ranked.length === 0
  const rowCount = rows.length + (fallbackVisible ? 1 : 0) + (recoveryVisible ? 1 : 0)

  useEffect(() => {
    if (selected >= rowCount) setSelected(Math.max(0, rowCount - 1))
  }, [rowCount, selected])

  const activateIndex = useCallback(
    (index: number) => {
      if (fallbackVisible && index === rows.length) {
        enterScope('sessions', query)
        return
      }
      if (recoveryVisible && index === rows.length + 1) {
        // Offer the way back rather than only describing it.
        navigate('/apps/detail/command-bar')
        onClose()
        return
      }
      const row = rows[index]
      if (!row) return
      if (scope) {
        ;(row as Result).onActivate()
        onClose()
        return
      }
      activateRoot(row as RankedRow)
    },
    [activateRoot, enterScope, fallbackVisible, navigate, onClose, query, recoveryVisible, rows, scope],
  )

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelected(i => (rowCount === 0 ? 0 : (i + 1) % rowCount))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelected(i => (rowCount === 0 ? 0 : (i - 1 + rowCount) % rowCount))
      } else if (e.key === 'Enter') {
        e.preventDefault()
        activateIndex(selected)
      } else if (e.key === 'Backspace' && query === '' && scope) {
        // Leaving a scope is Backspace on an empty input — the same gesture that
        // deletes a character, so it needs no separate key to learn.
        e.preventDefault()
        setScope(null)
        setSelected(0)
      }
    },
    [activateIndex, onClose, query, rowCount, scope, selected],
  )

  if (!open) return null

  const scopeName = scope === 'sessions' ? i18nT('apps.commandBar.cmd_search_sessions') : ''
  const listId = 'command-bar-list'
  const rowId = (i: number) => `command-bar-row-${i}`

  return createPortal(
    <div
      className="fixed left-0 right-0 z-[9999] flex items-start justify-center bg-bg/60 backdrop-blur-sm animate-rise"
      style={{ top: vv.offsetTop, height: vv.height }}
      // The backdrop is a click target for dismissal, not a control: the dialog role
      // belongs to the card below, and screen readers should skip this layer.
      role="presentation"
      // Dismiss only when the press lands on the backdrop ITSELF. Testing the target
      // beats stopping propagation on the card, which would put a mouse handler on a
      // non-interactive dialog element for no behavioural gain.
      onMouseDown={e => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className="w-full max-w-xl mx-4 bg-card border border-border rounded-xl shadow-xl overflow-hidden flex flex-col"
        style={{ marginTop: Math.round(vv.height * 0.12), maxHeight: Math.round(vv.height * 0.7) }}
        role="dialog"
        aria-modal="true"
        aria-label={i18nT('apps.commandBar.title')}
        // Escape belongs to the DIALOG, not the input. The focus trap's own Escape is
        // disabled because leaving a scope has to come first, and while the input was
        // the only focusable element putting the handler there was equivalent -- it is
        // not any more: Tab reaches the scope chip and the Retry button, and Escape
        // must dismiss from either. Keydown from the input bubbles here, so this is
        // one owner rather than two.
        onKeyDown={e => {
          if (e.key !== 'Escape') return
          e.preventDefault()
          // Inside a scope, Escape steps OUT of it rather than discarding the whole
          // search: the query the user typed is the expensive part, and Backspace on
          // an empty input is the only other way back, which nothing advertises.
          if (scope) {
            setScope(null)
            setSelected(0)
            inputRef.current?.focus()
            return
          }
          onClose()
        }}
      >
        <div className="flex items-center gap-2 px-3 py-2.5 border-b border-border">
          <Command size={15} className="lucide-inline text-muted shrink-0" />
          {scope && (
            <button
              type="button"
              onClick={() => {
                setScope(null)
                setSelected(0)
                inputRef.current?.focus()
              }}
              title={i18nT('apps.commandBar.leave_scope')}
              aria-label={i18nT('apps.commandBar.leave_scope')}
              className="shrink-0 max-w-[40%] truncate text-[11px] px-1.5 py-0.5 rounded bg-accent-subtle text-accent border-none cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
            >
              {scopeName}
            </button>
          )}
          <input
            ref={inputRef}
            autoFocus
            value={query}
            onChange={e => {
              setQuery(e.target.value)
              setSelected(0)
            }}
            onKeyDown={onKeyDown}
            placeholder={
              scope
                ? i18nT('apps.commandBar.placeholder_sessions')
                : i18nT('apps.commandBar.placeholder')
            }
            aria-label={i18nT('apps.commandBar.title')}
            // Selection stays on the input and is announced through
            // aria-activedescendant, so arrow keys never move DOM focus off it.
            role="combobox"
            aria-expanded={rowCount > 0}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={rowCount > 0 ? rowId(selected) : undefined}
            // The cue belongs on the active OPTION -- it says what Enter will do,
            // which a box round the field does not -- and this input is focused for
            // the whole life of the dialog, so an unconditional `focus-visible`
            // utility here renders a permanent box no launcher UI has. But
            // `aria-activedescendant` is omitted when there are no rows (entering
            // the sessions view before two characters are typed), and in THAT state
            // the option cue does not exist, so the field has to carry it or a
            // keyboard user sees nothing at all.
            className={`flex-1 min-w-0 bg-transparent border-none outline-none rounded text-[13px] text-text placeholder:text-muted${
              rowCount === 0 ? ' focus-visible:ring-1 focus-visible:ring-accent/40' : ''
            }`}
          />
        </div>

        {actionError && (
          <div
            role="alert"
            className="px-3 py-2 text-[12px] text-danger border-t border-border"
          >
            {actionError}
          </div>
        )}

        <div className="overflow-y-auto py-1" id={listId} role="listbox" aria-label={i18nT('apps.commandBar.title')}>
          {rowCount === 0 ? (
            <div className="px-3 py-6 text-center text-[12px] text-muted">
              {scope
                ? // Inside a scope the root's "no commands" copy would be a lie: the
                  // view searches sessions, and a fresh scope has no query yet.
                  scopedQuery.length < SESSIONS_MIN_CHARS
                  ? i18nT('apps.commandBar.keep_typing', { min: SESSIONS_MIN_CHARS })
                  : isFetching
                    ? i18nT('apps.commandBar.searching')
                    : // A rejected search leaves `data` undefined, which is
                      // indistinguishable from an empty result by row count alone --
                      // and reporting "no sessions match" for a failure tells the user
                      // their session does not exist. The error state has to be its
                      // own branch, with a way to try again.
                      isError
                      ? (
                          <span className="inline-flex items-center gap-2">
                            <span className="text-danger">
                              {i18nT('apps.commandBar.search_failed')}
                            </span>
                            <button
                              type="button"
                              onClick={() => void refetchSessions()}
                              className="text-[11px] px-1.5 py-0.5 rounded bg-accent-subtle text-accent border-none cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
                            >
                              {i18nT('apps.commandBar.retry')}
                            </button>
                          </span>
                        )
                      : i18nT('apps.commandBar.no_sessions')
                : i18nT('apps.commandBar.no_matches')}
            </div>
          ) : (
            <>
              {rows.map((row, i) => {
                const isRoot = !scope
                const rr = row as RankedRow
                const sr = row as Result
                const prev = i > 0 ? (rows[i - 1] as RankedRow) : undefined
                const header =
                  isRoot && (i === 0 || prev?.group !== rr.group) ? groupLabel(rr.group) : null
                return (
                  <div key={isRoot ? rr.id : sr.id}>
                    {header && (
                      <div className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wide text-muted">
                        {header}
                      </div>
                    )}
                    <div
                      id={rowId(i)}
                      role="option"
                      tabIndex={-1}
                      aria-selected={selected === i}
                      onMouseDown={() => activateIndex(i)}
                      onMouseEnter={() => setSelected(i)}
                      className={`flex items-center gap-2 px-3 py-1.5 cursor-pointer text-[13px] ${
                        selected === i ? 'bg-bg-hover text-text' : 'text-text'
                      }`}
                    >
                      <span className="shrink-0 text-muted">
                        {isRoot ? groupIcon(rr.group) : <MessageSquare size={14} className="lucide-inline" />}
                      </span>
                      <span className="flex-1 min-w-0">
                        <span className="block truncate">
                          {isRoot ? (
                            <Highlighted text={rr.title} indices={rr.indices} />
                          ) : (
                            <Highlighted text={sr.title} indices={sr.indices} />
                          )}
                        </span>
                        {/* Settings titles repeat across tabs ("Speed" exists on more
                            than one), so the row is only identifiable with its
                            subtitle rendered. */}
                        {isRoot && rr.subtitle && (
                          <span className="block truncate text-[11px] text-muted">{rr.subtitle}</span>
                        )}
                      </span>
                      {/* What activating this row will produce, right-aligned. Groups
                          always render as contiguous blocks under their own header
                          (rankRootRows sorts on groupOrder), so this is NOT about
                          groups interleaving -- an earlier version of this comment
                          claimed that and it was false. The label earns its place
                          because the only other per-row kind signal is the group
                          icon, which is self-describing only to a reader who has
                          already learned it, and because a block taller than the
                          dialog scrolls its header out of view. `view` rows are
                          called out separately from their group because they open a
                          surface inside the bar rather than doing something and
                          closing -- the one value the icon cannot convey. */}
                      {/* Root only. In the sessions view every row IS a session and the
                          scope chip above already says so, so a per-row label there
                          repeats the chip on every line without telling the reader
                          anything -- the mixing it disambiguates cannot happen. */}
                      {isRoot && (
                        <span className="shrink-0 text-[11px] text-muted">
                          {kindLabel(rr)}
                        </span>
                      )}
                      {/* The arrow gets a slot of its own on EVERY row, not just the
                          rows that have one. Rendered inline it pushed the label of a
                          `view` row left by its own width, so the labels stopped
                          sharing a right edge and the column read as misaligned. */}
                      <span className="shrink-0 w-[13px] flex justify-end">
                        {isRoot && rr.kind === 'view' && (
                          <ArrowRight size={13} className="lucide-inline text-muted" />
                        )}
                      </span>
                      {isRoot && pendingRow === rr.id && (
                        <Loader2
                          size={13}
                          aria-label={i18nT('apps.commandBar.working')}
                          className="lucide-inline text-muted shrink-0 animate-spin"
                        />
                      )}
                    </div>
                  </div>
                )
              })}
              {fallbackVisible && (
                <div
                  id={rowId(rows.length)}
                  role="option"
                  tabIndex={-1}
                  aria-selected={selected === rows.length}
                  onMouseDown={() => activateIndex(rows.length)}
                  onMouseEnter={() => setSelected(rows.length)}
                  className={`flex items-center gap-2 px-3 py-1.5 cursor-pointer text-[13px] border-t border-border ${
                    selected === rows.length ? 'bg-bg-hover' : ''
                  }`}
                >
                  {/* The root does not search content, so the typed text still has
                      somewhere to go: one Enter carries it into the sessions view
                      instead of scanning the corpus on every keystroke. */}
                  <MessageSquare size={14} className="lucide-inline text-muted shrink-0" />
                  <span className="flex-1 truncate text-muted">
                    {i18nT('apps.commandBar.fallback_sessions', { query })}
                  </span>
                </div>
              )}
              {/* Sessions is the only corpus this surface reaches. Naming the ones it
                  does not -- at the moment the user is looking for them, not only in
                  the App Store description they read once -- is what keeps a typed
                  artifact name from being a silent dead end. It is a row rather than
                  a note because stating the way back without offering it leaves the
                  user to close, navigate, find the app and disable it by hand. */}
              {recoveryVisible && (
                <div
                  id={rowId(rows.length + 1)}
                  role="option"
                  tabIndex={-1}
                  aria-selected={selected === rows.length + 1}
                  onMouseDown={() => activateIndex(rows.length + 1)}
                  onMouseEnter={() => setSelected(rows.length + 1)}
                  className={`flex items-start gap-2 px-3 py-1.5 cursor-pointer text-[11px] text-muted border-t border-border ${
                    selected === rows.length + 1 ? 'bg-bg-hover' : ''
                  }`}
                >
                  <Package size={13} className="lucide-inline shrink-0 mt-0.5" />
                  <span className="flex-1 min-w-0">
                    {i18nT('apps.commandBar.other_search_hint')}
                  </span>
                  <ArrowRight size={12} className="lucide-inline shrink-0 mt-0.5" />
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
