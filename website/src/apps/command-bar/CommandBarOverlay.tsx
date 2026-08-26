import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import {
  ArrowRight,
  Clock,
  Command,
  Cog,
  Loader2,
  MessageSquare,
  MessageSquarePlus,
  Package,
  RotateCcw,
  Search,
  Send,
  SunMoon,
  Terminal,
} from 'lucide-react'

import { api } from '../../api/client'
import { appNavTargets } from '../../appNav'
import { useAppDispatch, useAppSelector } from '../../store'
import { createSlot, setPendingInput, switchSlot } from '../../store/chatSlice'
import { Highlighted } from '../../components/commandPalette/Highlighted'
import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import { localizedSettingLabel } from '../../components/commandPalette/settingsSearchCore'
import { settingsRoute } from '../../components/commandPalette/settingsRoute'
import { settingsSubtitle } from '../../components/commandPalette/settingsTabLabel'
import { usePaletteActions } from '../../components/commandPalette/paletteActions'
import { appIcon } from '../../components/commandPalette/providers/appsProvider'
import { sessionStatus, useRecentsProvider } from '../../components/commandPalette/providers/recentsProvider'
import { useSessionsProvider } from '../../components/commandPalette/providers/sessionsProvider'
import type { Result } from '../../components/commandPalette/types'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { useVisualViewport } from '../../hooks/useVisualViewport'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'
import { useTheme } from '../../hooks/useTheme'
import { i18nT } from '../../i18n/t'
import { useLanguage } from '../../i18n/LanguageProvider'

import { loadUsage, recordUse, type UsageMap } from './frecency'
import { rankRootRows, type RankedRow, type RootGroup, type RootRow, type RootRowKind, type RowStatus } from './rootIndex'
import { useImeGuard } from '../../hooks/useImeGuard'

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
    case 'attention':
      return i18nT('apps.commandBar.group_attention')
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
 * by its group. There is deliberately no "Session" case: an attention row's column
 * carries its LIVE STATE instead, which is both more useful and the reason that
 * section exists — and a "Session" kind was considered for this surface once
 * before and dropped.
 */
function kindLabel(row: { kind: RootRowKind; group: RootGroup }): string | null {
  if (row.group === 'attention') return null
  if (row.kind === 'view') return i18nT('apps.commandBar.kind.view')
  if (row.group === 'apps') return i18nT('apps.commandBar.kind.app')
  if (row.group === 'settings') return i18nT('apps.commandBar.kind.setting')
  return i18nT('apps.commandBar.kind.command')
}

function groupIcon(group: RootGroup) {
  switch (group) {
    case 'attention':
      return <MessageSquare size={14} className="lucide-inline" />
    case 'commands':
      return <Terminal size={14} className="lucide-inline" />
    case 'apps':
      return <Package size={14} className="lucide-inline" />
    case 'settings':
      return <Cog size={14} className="lucide-inline" />
  }
}

/**
 * The right-hand column when a row has live state.
 *
 * One renderer for both row kinds -- a launcher row built from the store and a
 * session row built by a view's engine -- because the two would otherwise drift into
 * two treatments of the same fact. `pill` is reserved for what the user OWES the
 * session; a running session gets a dot, so "needs me" and "busy" never look alike.
 *
 * Sizing: only the pill and the dot are unshrinkable, because they are the signal and
 * they are a few pixels wide. Everything textual yields -- the label truncates and
 * the detail is dropped outright below the `sm` breakpoint -- so the ROW TITLE always
 * wins the contest for space. Held the other way round (a rigid accessory) a long
 * tool name collapsed the title on a 320px viewport, which inverts the point: the
 * title is what identifies the row, the detail is a bonus.
 */
function statusAccessory(status: RowStatus) {
  return (
    <span className="flex items-center gap-1.5 text-[11px] min-w-0 overflow-hidden">
      {status.pill ? (
        <span
          className="shrink-0 px-1.5 rounded text-[10px] uppercase tracking-wide"
          style={{ background: `var(${status.colorVar})`, color: 'var(--bg)' }}
        >
          {status.label}
        </span>
      ) : (
        <>
          <span
            className={`shrink-0 w-1.5 h-1.5 rounded-full${status.pulse ? ' animate-pulse' : ''}`}
            style={{ background: `var(${status.colorVar})` }}
          />
          <span className="truncate" style={{ color: `var(${status.colorVar})` }}>
            {status.label}
          </span>
        </>
      )}
      {status.detail && (
        <span className="hidden sm:inline truncate text-muted max-w-[180px]">
          {status.detail}
        </span>
      )}
    </span>
  )
}

/**
 * Every list position the bar can offer, as data.
 *
 * The bar used to hold its rows as one array plus two booleans, and read the extra
 * positions back out with index arithmetic (`rows.length`, `rows.length + 1`)
 * repeated in the activation switch, the render, and the id generator. That works
 * for two extras and stops working at four: each new one has to be inserted at the
 * same offset in three places, and getting it wrong activates a DIFFERENT row than
 * the one the user is looking at — a failure with no visual symptom until it fires.
 *
 * Positions are now one ordered list of tagged slots. Selection is an index into
 * it, and activation, the footer's action name and the rendered row all switch on
 * the same tag, so a slot cannot exist in one of those and not the others.
 */
type Slot =
  /** A launcher row from the root index. */
  | { key: string; tag: 'root'; row: RankedRow }
  /** A row produced by a scoped view's engine (session search, recents). */
  | { key: string; tag: 'result'; row: Result }
  /** Carry the typed text into the sessions view. */
  | { key: string; tag: 'fallback' }
  /** Hand the typed text to an agent — the active session, or a new one. */
  | { key: string; tag: 'ask' }
  /** The dead end's way out: the corpora this surface does not reach. */
  | { key: string; tag: 'recovery' }
  /** Re-run a scoped search that failed. */
  | { key: string; tag: 'retry' }
  /** Drop the query and fall back to the recent sessions listing. */
  | { key: string; tag: 'clear-query' }

/**
 * What Enter on this slot will do, named for the footer.
 *
 * The bar's whole promise is that Enter does something specific, and until this
 * existed nothing said what: the row carried its TYPE ("Command", "View") while
 * the verb — run it, open it, step into it — was left for the user to infer from
 * having pressed Enter before.
 */
function actionLabel(slot: Slot): string {
  switch (slot.tag) {
    case 'root':
      if (slot.row.kind === 'view') return i18nT('apps.commandBar.action_enter')
      if (slot.row.kind === 'navigate') return i18nT('apps.commandBar.action_open')
      return i18nT('apps.commandBar.action_run')
    case 'result':
      return i18nT('apps.commandBar.action_open_session')
    case 'fallback':
      return i18nT('apps.commandBar.action_search_sessions')
    case 'ask':
      return i18nT('apps.commandBar.action_ask')
    case 'recovery':
      return i18nT('apps.commandBar.action_open_app')
    case 'retry':
      return i18nT('apps.commandBar.retry')
    case 'clear-query':
      return i18nT('apps.commandBar.action_show_recent')
  }
}

/**
 * The section header this slot opens, or null when it continues the one above it.
 *
 * Root slots head on their group; view slots head on whatever the engine grouped
 * them by (the recents listing separates live sessions from history). The synthetic
 * slots — fallback, recovery, retry — head on nothing: they are one-offs at the
 * bottom of the list, and a header over a single row is noise.
 */
function headerOf(slot: Slot, prev?: Slot): string | null {
  if (slot.tag === 'root') {
    if (prev?.tag === 'root' && prev.row.group === slot.row.group) return null
    return groupLabel(slot.row.group)
  }
  if (slot.tag === 'result') {
    const label = slot.row.groupLabel
    if (!label) return null
    if (prev?.tag === 'result' && prev.row.groupLabel === label) return null
    return label
  }
  return null
}

/** Placeholder bar widths, descending so the block reads as text rather than a grid. */
const SKELETON_WIDTHS = ['58%', '46%', '34%'] as const

/**
 * The Enter keycap.
 *
 * A symbol rather than the word: the footer is a key hint, and every locale's
 * keyboard prints this glyph on the key itself. Held as a constant so the
 * untranslated-literal gate is not asked to judge a lone punctuation mark.
 */
const ENTER_KEY = '\u21B5'

/**
 * Separator between a session row's folder and its timestamp.
 *
 * A constant for the same reason as the keycap: a lone middle dot is not copy, and
 * asking the untranslated-literal gate to judge one produces a false positive.
 */
const META_SEP = ' \u00B7 '

/**
 * Key of the ask slot.
 *
 * A constant because three places have to agree on it: the slot itself, the
 * in-flight guard that refuses a second activation, and the spinner that says the
 * work is running. A literal repeated three times is how the spinner ends up
 * pointing at a row that is not the one working.
 */
const ASK_SLOT_KEY = 'slot:ask'

export default function CommandBarOverlay({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const ime = useImeGuard()
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
  /**
   * Generation of the current dialog session, bumped every time the bar closes.
   *
   * An ask that is still creating its session when the user dismisses the bar has
   * lost its claim on the dashboard: seeding a composer and navigating at that point
   * writes into whatever the user moved on to. `ChatPage` guards the same class of
   * race with its own `ownsLifecycle()` check and states the trade there -- an
   * abandoned request may leave an unused server slot, but it must not write shared
   * state or steal focus from its successor. This is that guard for this surface.
   *
   * While the bar is OPEN it cannot go stale: the dialog is modal with a real focus
   * trap, so the only thing that can change the active session in that window is our
   * own create.
   */
  const dialogRunRef = useRef(0)

  const { navigate } = usePaletteActions()
  const { resolved } = useLanguage()
  const { cycle: cycleTheme } = useTheme()
  const dispatch = useAppDispatch()
  // Live session state, read straight from the store the dashboard already keeps
  // current over its socket. This is what lets the root LEAD with the sessions that
  // owe the user something without issuing a request: the facts are already here,
  // and the alternative surfaces (sidebar, recents) are reading the same three.
  const liveSlots = useAppSelector(s => s.dashboard.slots)
  const unreadSlots = useAppSelector(s => s.dashboard.unreadSlots)
  const slotStatusDetail = useAppSelector(s => s.chat.slotStatusDetail ?? {})
  const simplifiedToolNames = useSimplifiedToolNames()
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
  // The sessions view's LISTING engine. Constructed here and inert: the hook reads
  // live slots out of the store and reads a localStorage preference, but issues no
  // request until `search()` is called, and the only call site is gated on the
  // scope below. Constructing it costs the root nothing; entering the view is what
  // makes it fetch.
  const recentSessions = useRecentsProvider()

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

  // Closing is what revokes an in-flight activation's claim. Bumped on the CLOSE edge
  // rather than the open one so work started in this session is invalidated the
  // moment the user walks away from it, not later when they happen to come back.
  useEffect(() => {
    if (!open) dialogRunRef.current += 1
  }, [open])

  // Teardown revokes too. The effect above is keyed on the `open` PROP, and an
  // unmount never sets it false -- the component is simply gone, its effects never
  // run again, and the in-flight callback still holds the ref OBJECT, so it would
  // compare equal and pass its own guard. This is the case a host that renders the
  // overlay conditionally produces, and it is the one a review found after the prop
  // edge was already covered.
  useEffect(() => () => {
    dialogRunRef.current += 1
  }, [])

  // A failure describes the row the user just activated, so it must not outlive the
  // query that produced it. The in-flight guard is deliberately NOT cleared here:
  // typing while work is resolving must not re-arm a second activation of it.
  useEffect(() => {
    setActionError(null)
  }, [query])

  const rootRows: RootRow[] = useMemo(() => {
    const rows: RootRow[] = []
    // The sessions that owe the user something, FIRST.
    //
    // `sessionStatus` marks exactly those with `style: 'pill'` — an approval to
    // grant, a question to answer — and everything else (running, unread, idle) with
    // a dot or nothing. Only the pill cases are lifted here, so this section is
    // usually absent: a launcher that always opens on a "Needs You" header teaches
    // the user to ignore it, and the whole value is that its presence means
    // something. A running session is not waiting on anyone and stays in the
    // sessions view where it belongs.
    for (const slot of liveSlots) {
      const st = sessionStatus(slot, unreadSlots, slotStatusDetail[slot.key], simplifiedToolNames)
      if (st.style !== 'pill' || !st.label || !st.colorVar) continue
      rows.push({
        id: `attention:${slot.key}`,
        title: slot.title || slot.key,
        group: 'attention',
        kind: 'invoke',
        icon: <MessageSquare size={14} className="lucide-inline" />,
        status: { colorVar: st.colorVar, label: st.label, detail: st.detail, pill: true },
        // Same activation the sidebar and the recents listing use, so a session
        // opened from here lands exactly where it lands from anywhere else.
        run: async () => {
          dispatch(switchSlot(slot.key))
          navigate('/chat')
        },
      })
    }
    rows.push(
      {
        id: 'command:new-session',
        title: i18nT('apps.commandBar.cmd_new_session'),
        group: 'commands',
        kind: 'invoke',
        icon: <MessageSquarePlus size={14} className="lucide-inline" />,
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
        icon: <SunMoon size={14} className="lucide-inline" />,
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
        icon: <Search size={14} className="lucide-inline" />,
        keywords: ['history', 'chat', 'conversation'],
      },
    )
    for (const target of appNavTargets(apps ?? [])) {
      rows.push({
        id: `app:${target.name}`,
        title: target.label,
        group: 'apps',
        kind: 'navigate',
        route: target.route,
        // The app's own art, through the chain the rail and the palette already
        // share. Filed by group alone every app row rendered the same package
        // outline, so the icon column told the reader only that these were apps --
        // which the group header above them and the label to their right both
        // already said.
        icon: appIcon(target),
      })
    }
    for (const entry of SETTINGS_REGISTRY) {
      rows.push({
        id: `setting:${entry.id}`,
        // The shared resolver, not a bare labelKey lookup: resolving the key
        // alone drops the fan-out suffix ("Bot Token (Discord)" → "Bot
        // Token"), rendering per-channel rows as indistinguishable titles.
        title: localizedSettingLabel(entry),
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
  }, [apps, cycleTheme, dispatch, liveSlots, navigate, resolved, simplifiedToolNames, slotStatusDetail, unreadSlots])

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
  /** The scoped SEARCH is armed only once the query is long enough to answer. */
  const searchArmed = scope === 'sessions' && scopedQuery.length >= SESSIONS_MIN_CHARS
  const { data: scopedResults, isFetching, isError, refetch: refetchSessions } = useQuery({
    queryKey: ['command-bar', 'sessions', scopedQuery],
    queryFn: () => Promise.resolve(sessions.search(scopedQuery)) as Promise<Result[]>,
    enabled: searchArmed,
    staleTime: 15_000,
  })

  /**
   * What the sessions view shows BEFORE a query narrows it: the recent sessions.
   *
   * Entering the view used to land on a centred "keep typing" sentence — a screen
   * with no rows, so no selection, so nothing Enter could do. That state is also
   * the only reason the input needs a focus box at all: the cue normally rides the
   * active option, and there was no option to ride. A view whose listing IS its
   * empty state has neither problem, and the listing is what the user is most often
   * after anyway — the session they were in a minute ago, reachable without
   * remembering a word from its title.
   *
   * `enabled` is what keeps the root request-free: the provider is inert until
   * `search()` runs, and this is its only call site.
   */
  const listingArmed = scope === 'sessions' && !searchArmed
  const { data: recentRows } = useQuery({
    queryKey: ['command-bar', 'recents'],
    queryFn: () => Promise.resolve(recentSessions.search('')) as Promise<Result[]>,
    enabled: listingArmed,
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

  const slots: Slot[] = useMemo(() => {
    if (scope === 'sessions') {
      const engine = searchArmed ? scopedResults : recentRows
      const out: Slot[] = (engine ?? []).map(row => ({ key: row.id, tag: 'result' as const, row }))
      if (searchArmed && isError) {
        // A failed search is not an empty one. The retry was a bare <button> inside
        // the empty-state paragraph, which the keyboard path that reached this state
        // could not get to without Tabbing out of the list.
        out.push({ key: 'slot:retry', tag: 'retry' })
      } else if (searchArmed && !isFetching && out.length === 0) {
        // A query that matched nothing still has somewhere to go — back to the
        // listing — so the view never bottoms out with nothing selectable.
        out.push({ key: 'slot:clear-query', tag: 'clear-query' })
      }
      return out
    }
    const out: Slot[] = ranked.map(row => ({ key: row.id, tag: 'root' as const, row }))
    if (query.trim().length > 0) {
      // The agent goes FIRST among the tail rows. Every other surface in this
      // product ends in saying something to one, so the typed text having somewhere
      // to go is not a corner-of-the-screen fallback for when search failed — it is
      // the general case, and the command list above it is the shortcut layer.
      out.push({ key: ASK_SLOT_KEY, tag: 'ask' })
      out.push({ key: 'slot:fallback', tag: 'fallback' })
      // The recovery row exists for the dead end — a typed query that matched
      // nothing — not for every keystroke. Riding the fallback's own condition put a
      // row about switching the feature off under every successful search, and
      // ArrowUp from the top wrapped selection straight onto it.
      if (ranked.length === 0) out.push({ key: 'slot:recovery', tag: 'recovery' })
    }
    return out
  }, [isError, isFetching, query, ranked, recentRows, scope, scopedResults, searchArmed])

  const rowCount = slots.length
  /**
   * True while a scoped engine owes us rows.
   *
   * Distinguished from "empty" because the two need opposite treatments: a pending
   * listing renders placeholder rows that hold the list's height, where the centred
   * "Searching…" line it replaces collapsed the panel and then jumped when results
   * landed.
   */
  const scopeLoading =
    scope === 'sessions' && rowCount === 0 && (isFetching || (listingArmed && recentRows === undefined))

  useEffect(() => {
    if (selected >= rowCount) setSelected(Math.max(0, rowCount - 1))
  }, [rowCount, selected])

  const activateIndex = useCallback(
    (index: number) => {
      const slot = slots[index]
      if (!slot) return
      switch (slot.tag) {
        case 'root':
          activateRoot(slot.row)
          return
        case 'result':
          slot.row.onActivate()
          onClose()
          return
        case 'fallback':
          enterScope('sessions', query)
          return
        case 'ask': {
          // A NEW session, never the active one's composer: `ChatPage` consumes
          // `pendingInput` by REPLACING the slot's draft and persisting it, so
          // inserting here would destroy a half-written message the user had not sent.
          // "Ask the agent" also fires from anywhere in the dashboard, where the
          // active session may be one the user last touched hours ago.
          //
          // The three steps are run HERE rather than through the shared
          // `newSessionWithToken` because that helper is fire-and-forget: its failure
          // path is a `console.error`, so a gateway that refuses the create would
          // leave the bar closing on nothing and the typed question gone. This row
          // carries a whole sentence the user composed, so it closes only once the
          // session exists, and a rejection keeps the bar open with the text still in
          // the field -- the same contract the New Session row already honours.
          if (pendingRow) return
          const text = query.trim()
          const run = dialogRunRef.current
          const owned = () => dialogRunRef.current === run
          setPendingRow(ASK_SLOT_KEY)
          // `activate: false` is what makes the rest of this safe, and it exists for
          // exactly this shape: the thunk's own docs say it creates the session
          // WITHOUT stealing focus so a caller that must finish setting the slot up
          // can do so before the user is able to type into it. The previous form
          // leaned on "create makes the new slot active", which is only true at the
          // instant it resolves -- and this callback can resolve long after the user
          // has moved on, at which point the seed lands in whatever they moved to.
          // Nothing is activated until we have both the slot's key and our claim.
          void dispatch(createSlot({ activate: false }))
            .unwrap()
            .then(
              async slot => {
                // The guard is released in `finally`, AFTER every await. Releasing it
                // up here left it open across the switch, so a second Enter during a
                // slow slot fetch started a second create -- two blank sessions from
                // one intent, which is the exact failure the guard exists to prevent.
                try {
                  if (!owned()) return
                  // `switchSlot.pending` activates immediately, so the seed that
                  // follows lands in THIS slot; a failing detail fetch afterwards does
                  // not undo the activation, which is why its rejection is ignored
                  // rather than surfaced.
                  await dispatch(switchSlot(slot.key)).unwrap().catch(() => {})
                  // Re-checked: the switch is another await, and the bar is still
                  // dismissable across it.
                  if (!owned()) return
                  dispatch(setPendingInput(text))
                  navigate('/chat')
                  onClose()
                } finally {
                  setPendingRow(null)
                }
              },
              () => {
                setPendingRow(null)
                if (!owned()) return
                setActionError(
                  i18nT('apps.commandBar.action_failed', {
                    action: i18nT('apps.commandBar.action_ask'),
                  }),
                )
              },
            )
          return
        }
        case 'recovery':
          // Offer the way back rather than only describing it.
          navigate('/apps/detail/command-bar')
          onClose()
          return
        case 'retry':
          void refetchSessions()
          return
        case 'clear-query':
          // Emptying the query is what re-arms the listing; the debounced copy has to
          // go with it or the view stays on the failed search for one more tick.
          setQuery('')
          setDebounced('')
          setSelected(0)
          inputRef.current?.focus()
          return
      }
    },
    [activateRoot, dispatch, enterScope, navigate, onClose, pendingRow, query, refetchSessions, slots],
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
        // Only the Enter branch is claimed — arrow navigation stays untouched.
        if (!ime.claimEnter(e)) return
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

  /**
   * What one slot puts in each of the row's four columns.
   *
   * Split from the shell below so every tag renders through the SAME shell: the
   * right-hand column only shares one edge if one element owns its width, and the
   * previous form (per-branch JSX) is how a `view` row's label ended up pushed left
   * by its own arrow.
   */
  const slotParts = (
    slot: Slot,
  ): { icon: ReactNode; title: ReactNode; subtitle?: ReactNode; accessory?: ReactNode; arrow?: boolean; dim?: boolean } => {
    switch (slot.tag) {
      case 'root': {
        const row = slot.row
        return {
          icon: row.icon ?? groupIcon(row.group),
          title: <Highlighted text={row.title} indices={row.indices} />,
          // Settings titles repeat across tabs ("Speed" exists on more than one), so
          // the row is only identifiable with its subtitle rendered — and when the
          // subtitle is what MATCHED, it is highlighted, because an unhighlighted row
          // in a filtered list reads as a row that should not be there.
          subtitle: row.subtitle
            ? row.matchField === 'subtitle'
              ? <Highlighted text={row.subtitle} indices={row.subtitleIndices ?? []} />
              : row.subtitle
            : undefined,
          accessory: (
            <>
              {/* The alias that put this row here. Without it a keyword hit rendered
                  with no highlight anywhere: typing `theme` listed settings rows whose
                  title and subtitle both lack the word, and the row offered the reader
                  no way to tell why it had matched. */}
              {row.matchField === 'keyword' && row.matchedKeyword && (
                <span className="truncate text-[11px] text-muted max-w-[140px]">
                  {row.matchedKeyword}
                </span>
              )}
              {/* Live state OUTRANKS the static kind label: on the one row where both
                  could apply, what the session is waiting for is the reason the row is
                  on screen and "Session" would be the reason it is not. */}
              {row.status ? (
                statusAccessory(row.status)
              ) : (
                <span className="shrink-0 text-[11px] text-muted">{kindLabel(row)}</span>
              )}
            </>
          ),
          arrow: row.kind === 'view',
        }
      }
      case 'result': {
        const row = slot.row
        // The live state a session row carries, mapped onto the same shape the
        // launcher rows use. This is the surface's own advantage and it was being
        // thrown away: the provider computes an approval pill, a pulsing "Thinking…"
        // and the running tool's name, and the row was rendering a folder and a clock.
        const status: RowStatus | undefined =
          row.statusLabel && row.statusColorVar
            ? {
                colorVar: row.statusColorVar,
                label: row.statusLabel,
                detail: row.statusDetail,
                pulse: row.statusPulse,
                pill: row.statusStyle === 'pill',
              }
            : undefined
        // Where it lives and when it was last touched — the two things that tell two
        // similarly-titled conversations apart. Yields the column to live state,
        // which is the more urgent fact about the same row.
        const meta = [row.folder, row.timestamp].filter(Boolean).join(META_SEP)
        return {
          icon: row.icon,
          title: <Highlighted text={row.title} indices={row.indices} />,
          subtitle: row.subtitle ? (
            <Highlighted text={row.subtitle} indices={row.subtitleIndices ?? []} />
          ) : undefined,
          accessory: status
            ? statusAccessory(status)
            : meta
              ? <span className="truncate text-[11px] text-muted max-w-[180px]">{meta}</span>
              : undefined,
        }
      }
      case 'ask':
        // Every other surface in this product ends in saying something to an agent, so
        // the typed text always has this way out — named with the text itself so the
        // row states what it will send rather than advertising a feature.
        return {
          icon: <Send size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.ask_agent', { query: query.trim() }),
          arrow: true,
          dim: true,
        }
      case 'fallback':
        // The root does not search content, so the typed text still has somewhere to
        // go: one Enter carries it into the sessions view instead of scanning the
        // corpus on every keystroke.
        return {
          icon: <Search size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.fallback_sessions', { query }),
          arrow: true,
          dim: true,
        }
      case 'recovery':
        // Sessions is the only corpus this surface reaches. Naming the ones it does
        // not -- at the moment the user is looking for them -- is what keeps a typed
        // artifact name from being a silent dead end.
        return {
          icon: <Package size={13} className="lucide-inline" />,
          title: i18nT('apps.commandBar.other_search_hint'),
          arrow: true,
          dim: true,
        }
      case 'retry':
        return {
          icon: <RotateCcw size={14} className="lucide-inline" />,
          title: <span className="text-danger">{i18nT('apps.commandBar.search_failed')}</span>,
        }
      case 'clear-query':
        // One row carrying both halves: the search found nothing, and the listing is
        // one Enter away. Split across a message and a control they were two things
        // to read; as a row it is one thing to do.
        return {
          icon: <Clock size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.no_match_show_recent', { query: scopedQuery }),
          dim: true,
        }
    }
  }

  const renderSlot = (slot: Slot): ReactNode => {
    const parts = slotParts(slot)
    return (
      <>
        <span className="shrink-0 w-4 flex justify-center text-muted">{parts.icon}</span>
        <span className="flex-1 min-w-0">
          <span className={`block truncate${parts.dim ? ' text-muted' : ''}`}>{parts.title}</span>
          {parts.subtitle && (
            <span className="block truncate text-[11px] text-muted">{parts.subtitle}</span>
          )}
        </span>
        {parts.accessory}
        {/* The arrow gets a slot of its own on EVERY row, not just the rows that have
            one. Rendered inline it pushed the label of a `view` row left by its own
            width, so the labels stopped sharing a right edge and the column read as
            misaligned. */}
        <span className="shrink-0 w-[13px] flex justify-end">
          {parts.arrow && <ArrowRight size={13} className="lucide-inline text-muted" />}
        </span>
        {/* The row doing awaited work says so. Both kinds that can be in flight are
            named here: an `invoke` launcher row, and the ask row. */}
        {((slot.tag === 'root' && pendingRow === slot.row.id) ||
          (slot.tag === 'ask' && pendingRow === ASK_SLOT_KEY)) && (
          <Loader2
            size={13}
            aria-label={i18nT('apps.commandBar.working')}
            className="lucide-inline text-muted shrink-0 animate-spin"
          />
        )}
      </>
    )
  }

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
        // 680px, up from 576: at the narrower width a settings row's title and its
        // tab subtitle both truncated on a 1440 screen, which is the one thing that
        // column exists to prevent. The panel scales in over 200ms — the same entrance
        // the rest of the shell's dialogs use — because a launcher that hard-cuts into
        // place reads as a repaint rather than as a surface arriving. The global
        // reduced-motion rule zeroes its duration.
        className="w-full max-w-[680px] mx-4 bg-card border border-border rounded-xl shadow-xl overflow-hidden flex flex-col animate-scale-in"
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
            <>
              <button
                type="button"
                onClick={() => {
                  setScope(null)
                  setSelected(0)
                  inputRef.current?.focus()
                }}
                title={i18nT('apps.commandBar.leave_scope')}
                aria-label={i18nT('apps.commandBar.leave_scope')}
                // No fill and no tint. A filled accent pill sitting against an
                // unpainted field is the loudest thing on the surface, and it is
                // labelling the state the user just chose — the one thing they already
                // know. Weight and a separator carry the same information: this word is
                // where you are, what follows is what you type. The focus ring stays,
                // and unlike the field's it only ever paints on Tab, so it is never the
                // permanent box.
                className="shrink-0 max-w-[40%] truncate text-[13px] text-text bg-transparent border-none p-0 cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 rounded"
              >
                {scopeName}
              </button>
              <span aria-hidden className="shrink-0 text-muted select-none">
                ›
              </span>
            </>
          )}
          <input
            ref={inputRef}
            autoFocus
            value={query}
            onChange={e => {
              setQuery(e.target.value)
              setSelected(0)
            }}
            {...ime.bindComposition()}
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
            // utility here renders a permanent box no launcher UI has. The residual
            // case is a list with no rows at all: `aria-activedescendant` is omitted
            // there, so no option exists to carry the cue and the field has to, or a
            // keyboard user sees nothing.
            //
            // Two things changed about that residual. It is now nearly unreachable --
            // every state that used to produce it (a fresh sessions view, a failed
            // search, a search that matched nothing) carries rows of its own, leaving
            // only an instance with no sessions whatsoever. And what it paints is a
            // neutral hairline rather than an accent ring: an accent-coloured box
            // around the one element that is ALWAYS focused read as the loudest thing
            // on a surface whose entire visual weight is supposed to sit on the
            // selected row.
            className={`flex-1 min-w-0 bg-transparent border-none outline-none rounded text-[13px] text-text placeholder:text-muted${
              rowCount === 0 ? ' focus-visible:ring-1 focus-visible:ring-border-strong' : ''
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
            scopeLoading ? (
              // Placeholder rows rather than a centred "Searching…" line. The line
              // collapsed the panel to one text height and then jumped when results
              // landed; these hold roughly the space the rows will occupy, so
              // entering a view is one movement instead of two.
              <>
                <div aria-hidden className="px-3 py-1">
                  {SKELETON_WIDTHS.map((w, n) => (
                    <div key={n} className="flex items-center gap-2.5 py-2">
                      <span className="shrink-0 w-4 h-4 rounded bg-bg-hover" />
                      <span className="h-3 rounded bg-bg-hover" style={{ width: w }} />
                    </div>
                  ))}
                </div>
                {/* The placeholder bars are decoration and hidden from AT, so the
                    state they depict has to be said out loud somewhere. */}
                <span role="status" className="sr-only">
                  {i18nT('apps.commandBar.searching')}
                </span>
              </>
            ) : (
              // Every other state now carries rows of its own, so this is the one
              // case left: a corpus that is genuinely empty.
              <div role="status" className="px-3 py-6 text-center text-[12px] text-muted">
                {scope ? i18nT('apps.commandBar.no_sessions') : i18nT('apps.commandBar.no_matches')}
              </div>
            )
          ) : (
            slots.map((slot, i) => {
              const header = headerOf(slot, slots[i - 1])
              return (
                <div key={slot.key}>
                  {header && (
                    <div className="px-3 pt-2 pb-0.5 text-[10px] uppercase tracking-wide text-muted">
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
                    className={`flex items-center gap-2.5 px-3 py-2 cursor-pointer text-[13px] ${
                      selected === i ? 'bg-bg-hover text-text' : 'text-text'
                    }`}
                  >
                    {renderSlot(slot)}
                  </div>
                </div>
              )
            })
          )}
        </div>

        {/* What Enter does, named. The bar's promise is that Enter does something
            specific to the highlighted row, and nothing said what: the row carried
            its TYPE ("Command", "App") while the verb was left to be inferred from
            having pressed Enter before. Rendered only when a row exists, because
            with none there is no action to name. Kept to one line of muted text and
            a keycap — the panel's weight belongs on the selected row. */}
        {rowCount > 0 && (
          <div className="flex items-center justify-end gap-2 px-3 py-1.5 border-t border-border text-[11px] text-muted">
            <span className="truncate">{actionLabel(slots[Math.min(selected, rowCount - 1)])}</span>
            <span className="shrink-0 px-1 rounded border border-border leading-4">{ENTER_KEY}</span>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
