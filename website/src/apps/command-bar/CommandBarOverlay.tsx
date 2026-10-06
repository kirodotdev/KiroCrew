import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowRight,
  Check,
  Clock,
  Command,
  Cog,
  Component,
  Folder,
  GitMerge,
  Loader2,
  MessageSquare,
  MessageSquarePlus,
  Package,
  RotateCcw,
  Search,
  Send,
  ScanEye,
  SunMoon,
  Terminal,
} from 'lucide-react'

import { api } from '../../api/client'
import { commandFolderName, fileSessionInCommandFolder } from './sessionFolder'
import type { ChatFolderRow } from './sessionFolder'
import type { ChatFolder } from '../../types'
import { appNavTargets } from '../../appNav'
import { useAppDispatch, useAppSelector, useAppStore } from '../../store'
import { createSlot, setPendingInput, switchSlot, requestFolderReveal } from '../../store/chatSlice'
import { focusComposerForOpenedSession } from '../../pages/chat/composerFocus'
import { findReport } from '../../utils/errorReport'
import { errMessage } from '../../utils/thunkError'
import ErrorNotice from '../../components/ErrorNotice'
import { Highlighted } from '../../components/commandPalette/Highlighted'
import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import { localizedSettingLabel, scoreSettingEntry, settingEntryOffered } from '../../components/commandPalette/settingsSearchCore'
import { useSettingsSearchGovernance } from '../../components/commandPalette/useSettingsSearchGovernance'
import { settingsRoute } from '../../components/commandPalette/settingsRoute'
import type { SettingEntry } from '../../components/commandPalette/settingsTypes'
import { settingsSubtitle } from '../../components/commandPalette/settingsTabLabel'
import { usePaletteActions } from '../../components/commandPalette/paletteActions'
import { appIcon } from '../../components/commandPalette/providers/appsProvider'
import {
  isEmptyNewSlot,
  recencyEpoch,
  sessionStatus,
  useRecentsProvider,
} from '../../components/commandPalette/providers/recentsProvider'
import { createArtifactsProvider } from '../../components/commandPalette/providers/artifactsProvider'
import type { ArtifactsResponse } from '../../components/commandPalette/providers/artifactsProvider'
import { createFoldersProvider, FOLDERS_STALE_MS } from './foldersProvider'
import { createMatesProvider, matesIcon, mateRoute } from './matesProvider'
import type { KiroCrewAgent } from '../../components/AgentSelector'
import { useFolderSortRead, type FolderSortConfigBody } from '../../hooks/useFolderSortMode'
import { useSessionsProvider } from '../../components/commandPalette/providers/sessionsProvider'
import type { Result } from '../../components/commandPalette/types'
import { resolveCopyTarget, type CopyableRow } from '../../components/commandPalette/copyTarget'
import { copyToClipboard } from '../../utils/clipboard'
import { platformShortcut } from '../../utils/platform'
import { buildShareableUrl } from '../../utils/shareUrl'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { useVisualViewport } from '../../hooks/useVisualViewport'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'
import { useTheme } from '../../hooks/useTheme'
import { i18nT } from '../../i18n/t'
import { useLanguage } from '../../i18n/LanguageProvider'

import { SETTING_ROW_PREFIX, loadUsage, recordUse, type UsageMap } from './frecency'
import { rankRootRows, type OwnMatch, type RankedRow, type RootGroup, type RootRow, type RootRowKind, type RowStatus } from './rootIndex'
import {
  argumentIsValid,
  contributedCommands,
  resolvePrompt,
  type ContributedCommand,
} from './contributedCommands'
import { useImeGuard } from '../../hooks/useImeGuard'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_CREW } from '../../utils/previewFlags'
import { invalidateFoldersWhenIdle } from '../../api/chatFoldersWrite'

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
type Scope = null | 'sessions' | 'artifacts' | 'folders' | 'mates'

const SESSIONS_MIN_CHARS = 2
/**
 * Shortest query the artifacts view will send.
 *
 * Two characters for a different reason than the sessions threshold above, which
 * mirrors a backend floor that returns nothing below it. The artifacts endpoint
 * answers a one-character name query perfectly well — what it costs is a scan over
 * every artifact's metadata to return most of the corpus, which is what the view
 * already shows for free when the query is empty. So the floor buys a cheaper
 * keystroke, not a correct answer.
 */
const ARTIFACTS_MIN_CHARS = 2
/**
 * Rows the artifacts view renders, listing and search alike.
 *
 * The endpoint returns the whole matching set with no page size, so this is the
 * only thing between a heavy instance and a thousand-row option list inside a
 * dialog. Sized to the list's own height rather than to the corpus: a name search
 * that needs more than this many rows to find its artifact needs a narrower query,
 * and the listing state is a shortcut to something recent, not an inventory.
 */
const ARTIFACTS_ROW_LIMIT = 12
/**
 * How long the crewmate roster stays fresh inside one visit.
 *
 * The same 15s the views above use, deliberately rather than something longer. The
 * argument for longer was that this corpus changes on a human timescale -- a crew is
 * added from the Crewmates page, not by an agent's turn -- but that gets the one case
 * that matters backwards: the human who just added a crew is the human now waiting
 * for it to appear, and a window sized to "nobody is watching" is longest exactly
 * when somebody is. Matching every sibling view also removes a special case with no
 * reader behind it.
 *
 * The fields that DO change per turn -- whether a mate is working -- are not in this
 * payload at all; they come from the live slot frames, which are never cached.
 */
const MATES_STALE_MS = 15_000
const DEBOUNCE_MS = 150

/**
 * Sessions the root lifts into its `recent` group.
 *
 * Three, because the value of this group is that the reader does not have to READ
 * it — at three rows the one they want is recognised at a glance and reached with
 * one arrow key, and the group costs the commands below it almost nothing. Sized to
 * the glance, not to the corpus: a fourth row buys a little more coverage and takes
 * the "no reading required" property with it, and the whole corpus is one row below
 * under Search Sessions.
 */
const RECENT_SESSION_ROWS = 3

/**
 * A settings row's matcher: the settings scorer decides whether and how well it
 * matches, and its tier says which field to draw the hit on.
 */
function settingRowMatch(query: string, entry: SettingEntry): OwnMatch | null {
  const hit = scoreSettingEntry(query, entry)
  if (!hit) return null
  if (hit.tier === 'label') return { score: hit.score, indices: hit.indices, field: 'title' }
  return {
    score: hit.score, indices: [], field: hit.matchedKeyword ? 'keyword' : 'subtitle',
    matchedKeyword: hit.matchedKeyword, discounted: hit.tier === 'corpus',
  }
}

function groupLabel(group: RootGroup): string {
  switch (group) {
    case 'attention':
      return i18nT('apps.commandBar.group_attention')
    case 'recent':
      // Its own header key beside `group_attention`, not the recents listing's bare
      // "Recent" (which `group_recent` already carries for the artifacts view): these
      // rows are the same session objects as "New Session" and "Search Sessions"
      // beside them, and a header naming the group ("Recent sessions") says so, where
      // a lone adjective borrowed from another surface reads as a different thing.
      return i18nT('apps.commandBar.group_recent_sessions')
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
function kindLabel(
  row: { kind: RootRowKind; group: RootGroup; appLabel?: string },
  queryActive = false,
): string | null {
  // An attention row's column carries its LIVE STATE, which is both more useful than
  // a kind word and the reason that section exists. It is never empty: a row reaches
  // this group only because its status is a pill, so the renderer always has one to
  // show and never falls through to this label.
  if (row.group === 'attention') return null
  // A recent row is named by its own group header and its session glyph, and its
  // right-hand column belongs to whatever the session is DOING. Labelling it
  // "Command" would be both wrong and the widest thing on the row.
  //
  // Under a QUERY there is no header (the list is one ranked block, so it carries
  // none), and an idle session has no live state either — which left the row as a
  // glyph and a title while every row beside it still showed its word. So the group's
  // own name stands in, in the column that is empty anyway. The idle page is
  // unchanged: the header above the row says this there.
  if (row.group === 'recent') return queryActive ? groupLabel('recent') : null
  if (row.kind === 'view') return i18nT('apps.commandBar.kind.view')
  if (row.group === 'apps') return i18nT('apps.commandBar.kind.app')
  if (row.group === 'settings') return i18nT('apps.commandBar.kind.setting')
  const kind = i18nT('apps.commandBar.kind.command')
  // Provenance ahead of the kind for a contributed row. Composed with the separator this
  // column already uses for folder/timestamp rather than a new string, so no catalog
  // learns a sentence about attribution -- the app's own name is data, not copy.
  return row.appLabel ? `${row.appLabel}${META_SEP}${kind}` : kind
}

/**
 * The root list's own row model, expressed as something the copy layer can read.
 *
 * The root index is not the palette's `Result` — it predates the declarative Enter
 * matrix and says where a row goes with `kind` + `route`. That is the same fact in
 * different words, so the adapter is a restatement rather than new wiring, and every
 * `navigate` row in the index (each settings row, each app row, each page row) becomes
 * copyable without touching the place that builds it.
 *
 * The other kinds have no address on purpose. A `view` row opens a surface INSIDE the
 * bar, so there is nothing to hand anybody; `invoke` runs a callback; `prompt` is a
 * question the reader has not answered yet.
 */
function rootRowCopyable(row: RootRow): CopyableRow {
  return row.kind === 'navigate' && row.route
    ? { enter: { kind: 'navigate', route: row.route } }
    : {}
}

function groupIcon(group: RootGroup) {
  switch (group) {
    case 'attention':
      return <MessageSquare size={14} className="lucide-inline" />
    case 'recent':
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
  /**
   * Carry the typed text into the artifacts view.
   *
   * A separate tag rather than a scope field on `fallback` so the footer's action
   * name, the rendered row and activation each switch on the tag and cannot
   * disagree about which corpus the Enter reaches.
   */
  | { key: string; tag: 'fallback-artifacts' }
  /**
   * Carry the typed text into the folders view.
   *
   * Its own tag for the reason the artifacts tag above gives: the footer's action
   * name, the rendered row and the activation all switch on the tag, so none of
   * them can disagree about which corpus the Enter reaches.
   */
  | { key: string; tag: 'fallback-folders' }
  /**
   * Carry the typed text into the crewmates view.
   *
   * Its own tag for the reason the two above give: the footer's action name, the
   * rendered row and the activation all switch on the tag, so none of them can
   * disagree about which corpus the Enter reaches.
   */
  | { key: string; tag: 'fallback-mates' }
  /** Hand the typed text to an agent — the active session, or a new one. */
  | { key: string; tag: 'ask' }
  /** The dead end's way out: the corpora this surface does not reach. */
  | { key: string; tag: 'recovery' }
  /** Re-run a scoped search that failed. */
  | { key: string; tag: 'retry' }
  /** Re-run an artifact search that failed. */
  | { key: string; tag: 'retry-artifacts' }
  /** Re-run a folder listing that failed. */
  | { key: string; tag: 'retry-folders' }
  /** Re-run a crewmate listing that failed. */
  | { key: string; tag: 'retry-mates' }
  /** Drop the query and fall back to the recent sessions listing. */
  | { key: string; tag: 'clear-query' }
  /**
   * Drop the query and fall back to the whole folder list.
   *
   * Separate from `clear-query` because the two rows name different destinations:
   * a session listing is the RECENT ones, while the folder listing is all of them
   * in sidebar order. One tag would have to read the live scope to pick the verb,
   * and `actionLabel` is pure over the slot precisely so it cannot.
   */
  | { key: string; tag: 'clear-query-folders' }
  /**
   * Drop the query and fall back to the whole crew roster.
   *
   * Separate from the two above for the reason they are separate from each other:
   * the destination is named in the row's own words, and `actionLabel` is pure over
   * the slot precisely so it cannot read the live scope to pick between them.
   */
  | { key: string; tag: 'clear-query-mates' }
  /**
   * The reader has NO crew: say so, and go make one.
   *
   * A row rather than the centred empty-state sentence this replaces, because that
   * sentence named the Crewmates page and gave no way to reach it -- "nothing looks
   * like a link". The empty state of a view whose whole subject is a collection the
   * reader creates is the one place an action belongs most.
   */
  | { key: string; tag: 'open-crewmates' }

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
      // A `prompt` row steps into a field rather than acting, so Enter is named for
      // proceeding. Calling it "Run" would promise that this Enter approves or
      // merges something, which is the one thing it must not be read as -- but
      // reusing the view row's "Open View" was its own false promise: no view opens
      // in either shape, and for an argument-less command that Enter creates a
      // session. "Continue" is the one word true of both, and it commits to nothing
      // the next step does not do.
      if (slot.row.kind === 'prompt') return i18nT('apps.commandBar.action_continue')
      if (slot.row.kind === 'navigate') return i18nT('apps.commandBar.action_open')
      return i18nT('apps.commandBar.action_run')
    case 'result':
      // An artifact row, a folder row and a session row are the same SHAPE and three
      // different promises, and the footer names the promise. Keyed off the producing
      // provider rather than the live scope because this function is pure over the
      // slot — and because the row carries where it came from, while the scope is
      // state next to it that a future view could disagree with.
      //
      // A folder gets `action_open` rather than "Run": pressing Enter reveals it in
      // the sidebar, and "Run" is the strongest verb this footer has, reserved for
      // the rows that approve or merge.
      if (slot.row.providerId === 'artifacts') return i18nT('apps.commandBar.action_open_artifact')
      if (slot.row.providerId === 'folders') return i18nT('apps.commandBar.action_open')
      // Named for the DESTINATION, not the object: Enter lands on the mate's own chat
      // thread, and a bare "Open" would leave a reader guessing between that and a
      // settings page for the crew. The verb is deliberately not "Run" — that is this
      // footer's strongest word, reserved for the rows that approve or merge.
      if (slot.row.providerId === 'mates') return i18nT('apps.commandBar.action_open_mate')
      return i18nT('apps.commandBar.action_open_session')
    case 'fallback':
      return i18nT('apps.commandBar.action_search_sessions')
    case 'fallback-artifacts':
      return i18nT('apps.commandBar.action_search_artifacts')
    case 'fallback-folders':
      return i18nT('apps.commandBar.action_search_folders')
    case 'fallback-mates':
      return i18nT('apps.commandBar.action_search_mates')
    case 'ask':
      return i18nT('apps.commandBar.action_ask')
    case 'recovery':
      return i18nT('apps.commandBar.action_open_app')
    case 'retry':
    case 'retry-artifacts':
    case 'retry-folders':
    case 'retry-mates':
      return i18nT('apps.commandBar.retry')
    case 'clear-query':
      return i18nT('apps.commandBar.action_show_recent')
    case 'clear-query-folders':
      return i18nT('apps.commandBar.action_show_all_folders')
    case 'clear-query-mates':
      return i18nT('apps.commandBar.action_show_all_mates')
    case 'open-crewmates':
      // Names the DESTINATION, not the bare verb. "Open" on the one row a crew-less
      // reader ever sees does not say open what.
      return i18nT('apps.commandBar.action_open_crewmates')
  }
}

/**
 * The section header this slot opens, or null when it continues the one above it.
 *
 * Root slots head on their group; view slots head on whatever the engine grouped
 * them by (the recents listing separates live sessions from history). The synthetic
 * slots — fallback, recovery, retry — head on nothing: they are one-offs at the
 * bottom of the list, and a header over a single row is noise.
 *
 * `queryActive` turns the root's headers OFF, because under a query the root is one
 * list ranked by match, not blocks filed by group — so a group can appear, be left,
 * and appear again, and a header per transition would print "Commands" twice with an
 * app between them and describe nothing. Each row still names its own kind in its
 * right-hand column, which is where a ranked list carries that fact. View slots keep
 * their headers either way: an engine's grouping is a fact about its results, not a
 * filing order the query just replaced.
 */
function headerOf(slot: Slot, prev?: Slot, queryActive = false): string | null {
  if (slot.tag === 'root') {
    if (queryActive) return null
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
 * The copy chord, spelled for the machine the reader is on.
 *
 * Through the product's own formatter rather than a literal, because the chord is not
 * the same everywhere: a hardcoded glyph would name a key Windows and Linux readers do
 * not have.
 */
const COPY_KEY = platformShortcut('Cmd+C')

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

/**
 * Glyphs a contributed command may name, and the fallback when it names none.
 *
 * An allowlist rather than a URL or inline SVG the app supplies, for two reasons
 * that both matter more than the extra vocabulary: the root promises to issue no
 * request, and a glyph that must be fetched breaks that promise on every open; and
 * an app-supplied SVG is app-authored markup rendered inside the host's own
 * surface. The set is small on purpose and grows by pull request, which is a cheap
 * ask compared to either alternative.
 */
const CONTRIBUTED_ICONS: Record<string, ReactNode> = {
  Check: <Check size={14} className="lucide-inline" />,
  Command: <Command size={14} className="lucide-inline" />,
  GitMerge: <GitMerge size={14} className="lucide-inline" />,
  Package: <Package size={14} className="lucide-inline" />,
  ScanEye: <ScanEye size={14} className="lucide-inline" />,
  Search: <Search size={14} className="lucide-inline" />,
  Send: <Send size={14} className="lucide-inline" />,
  Terminal: <Terminal size={14} className="lucide-inline" />,
}

/** The named glyph, or the generic command one when the name is unknown. */
function contributedIcon(name: string): ReactNode {
  // `Object.hasOwn`, not a plain index with `??`. An INHERITED key is not nullish, so
  // `CONTRIBUTED_ICONS['__proto__']` yields an object and `CONTRIBUTED_ICONS['constructor']`
  // a function -- neither triggers the fallback, and both are then handed to React as a
  // child, which throws. `icon` is deliberately unvalidated beyond being a string ("an
  // unknown name falls back"), so any name reaches this line, and the crash takes the
  // whole overlay down on every open rather than degrading that one row.
  return Object.hasOwn(CONTRIBUTED_ICONS, name) ? (
    CONTRIBUTED_ICONS[name]
  ) : (
    <Terminal size={14} className="lucide-inline" />
  )
}

export default function CommandBarOverlay({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const ime = useImeGuard()
  /**
   * Whether the Crewmates preview is on for this reader.
   *
   * This bar is an INGRESS to `/members`, and that page is registered with
   * `previewFlag: PREVIEW_CREW` — so every other door to it applies this gate, and a
   * surface that did not would advertise a page the operator has not opted into. The
   * app ships `defaultEnabled: true`, so without this a default install shows
   * `Search Crewmates` in the root, carries a typed query into it, and lets Enter
   * reach `/members` while the page itself is supposed to be invisible.
   *
   * It gates all THREE rows this feature adds — the view row, the fallback row and the
   * empty-state row — because each one is independently reachable.
   */
  const crewPreview = usePreviewFlag(PREVIEW_CREW)
  const vv = useVisualViewport()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [scope, setScope] = useState<Scope>(null)
  /**
   * The contributed command whose ARGUMENT the field is currently collecting.
   *
   * A second navigation state beside `scope`, and deliberately not folded into it:
   * a scope is a place to search, this is a question being asked, and the two
   * differ in what Enter means. They share the rest of the contract — the chip
   * naming where you are, Escape and Backspace stepping back out — so the sites
   * below read `scope ?? argCommand` rather than growing a second copy of each.
   *
   * Holds the whole command rather than an id: it carries the placeholder, the
   * pattern and the prompt template, and re-deriving those from the app list on
   * every keystroke would let a mid-flight app disable change what the field the
   * reader is typing into is about to run.
   */
  const [argCommand, setArgCommand] = useState<ContributedCommand | null>(null)
  //
  // Whether the resolved-prompt preview is taller than its box. Measured, not derived
  // from the prompt's length: wrapping is what decides overflow, so a character or
  // line count would both over- and under-report. Only used to warn that the
  // instruction continues out of sight -- never to gate the send, which stays the
  // reader's call.
  const previewRef = useRef<HTMLPreElement | null>(null)
  const [previewClipped, setPreviewClipped] = useState(false)

  const [selected, setSelected] = useState(0)
  /**
   * A keyboard Enter that landed in the debounce window, held until the rows answer
   * the query it was pressed against -- and no later one.
   *
   * A scoped view ranks from the DEBOUNCED query, so for one interval after a
   * keystroke its rows answer the previous one. An Enter then names a row built from
   * the old query, and this latch is the launcher's primary gesture -- type a name,
   * confirm it -- waiting for the rows to catch up rather than being eaten. The latch
   * remembers the exact query the reader confirmed and the row they had selected: it
   * fires once, on the live rows, only when the live query is still that query, the
   * selection is still that row, and the top of the list is a settled result. Any
   * further input, any selection move, a pointer activation, Escape, leaving the
   * scope, or closing the bar drops it instead -- a latch that outlived the reader's
   * confirmation would open a query they never confirmed, or a row they moved off of.
   * A read that fails with no cached rows leaves a retry row at the top, which is not
   * a result, so the latch drops on that dead end; a refetch that fails while the
   * confirmed query's rows are still cached fires those rows -- they answer the query
   * the reader confirmed, exactly as a fresh Enter would. `null` when no Enter is
   * pending.
   */
  const [pendingEnter, setPendingEnter] = useState<{ query: string; index: number; key: string } | null>(null)
  const [usage, setUsage] = useState<UsageMap>(() => loadUsage())
  // The rows every other settings search withholds, withheld here too, from
  // cached answers only: the root issues no request.
  const settingsGovernance = useSettingsSearchGovernance({ fetch: false })
  const [actionError, setActionError] = useState<string | null>(null)
  /**
   * What the last copy did, held until the reader moves.
   *
   * A copy is the one action on this surface with NO observable result: the bar looks
   * identical afterwards, the clipboard is not visible, and the reader finds out
   * whether it worked when they paste somewhere else. So it is stated here, and it is
   * stated from `copyToClipboard`'s own return value rather than from having called
   * it -- that helper explicitly warns that a tick over an unchanged clipboard is
   * worse than no affordance at all, and it fails for real on a plain-HTTP LAN
   * gateway, where the async clipboard API is unavailable.
   */
  const [copyNotice, setCopyNotice] = useState<{ text: string; what?: string } | null>(null)
  /**
   * A clipboard write that did NOT land.
   *
   * Held apart from `copyNotice` because it is a different kind of thing: a failed
   * write is an error, and an error renders through `ErrorNotice` like every other one
   * on this surface. A copy with no target is not an error -- the reader asked a row
   * with no address for its address -- so it stays on the status line.
   *
   * Carries `what` as well as the message, because the message names a REMEDY -- select
   * the text and copy it by hand -- and this is the one path where the text is not on
   * screen: the clipboard refused, so nothing else put it there. An instruction that
   * points at nothing is worse than no instruction.
   */
  const [copyError, setCopyError] = useState<{ text: string; what: string } | null>(null)
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
  // A store HANDLE for the session rows' post-switch focus: by the time a
  // `switchSlot` settles this bar has closed, so only the store can still say
  // which slot is active. Read, never subscribed -- the selectors below are the
  // subscriptions.
  const store = useAppStore()
  // Live session state, read straight from the store the dashboard already keeps
  // current over its socket. This is what lets the root LEAD with the sessions that
  // owe the user something without issuing a request: the facts are already here,
  // and the alternative surfaces (sidebar, recents) are reading the same three.
  const liveSlots = useAppSelector(s => s.dashboard.slots)
  const unreadSlots = useAppSelector(s => s.dashboard.unreadSlots)
  const slotStatusDetail = useAppSelector(s => s.chat.slotStatusDetail ?? {})
  const simplifiedToolNames = useSimplifiedToolNames()
  // `aria-modal` is a promise that Tab cannot reach the page behind the dialog, so
  // the trap has to be real. Escape is owned by the dialog panel's own keydown
  // handler below, which needs it to pop a scope before it closes the bar (and
  // claims it through the IME latch first).
  useDialogFocusTrap(dialogRef, onClose, { handleEscape: false })
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

  // The folder list is READ the same way and for a sharper reason: `GET
  // /api/chat/folders` walks the on-disk session list synchronously to count archived
  // sessions per folder, so fetching it here would pay for a filesystem scan on every
  // command run to learn what the sidebar's own cache already holds (the WebSocket
  // seeds this key from the folder tree). A cold cache falls back to one fetch inside
  // `fileSessionInCommandFolder`.
  const { data: chatFolders } = useQuery({
    queryKey: ['chat-folders'],
    queryFn: () => api.chatFolders(),
    enabled: false,
  })
  // Held in a ref because the filing runs from an async callback, long after the render
  // that read the cache.
  const chatFoldersRef = useRef<unknown>(chatFolders)
  chatFoldersRef.current = chatFolders
  const queryClient = useQueryClient()

  // The person's folder sort mode (`dashboard.folder_sort`), read the same way as the
  // two entries above — a SUBSCRIBER to the shared `['kirocrewConfig']` key, never a
  // fetch: the shell's own read of that key holds the entry from boot and the
  // WebSocket invalidates it on a config change, so fetching here would be the
  // request-on-open this surface exists to avoid, for a value that is already in the
  // cache. Read through the sidebar hook's own derivation, so the Folders view
  // below lists in the order the sidebar draws — and says the same failed read the
  // sidebar says, above its list, when there is no body to draw from (there is no
  // sidebar on this surface to say it). A body on hand is drawn and acted on,
  // whatever the shell's last refetch did.
  const kirocrewConfigQuery = useQuery<FolderSortConfigBody>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: false,
  })
  const { mode: folderSortMode, error: folderSortError } = useFolderSortRead(kirocrewConfigQuery)

  /**
   * The artifacts view's engine.
   *
   * Built here from the palette provider's factory rather than taken from its hook,
   * for two reasons that both matter to this surface. The request is ours: the hook
   * asks for `content=1` and `snippet=1`, and `snippet=1` alone makes the server
   * read every listed artifact's body to build a preview — so a name search that
   * left it on would pay for the full-corpus content read it was avoiding. And the
   * navigation is ours: every other row in this file leaves through
   * `usePaletteActions().navigate`, and the hook would bring a second router
   * mechanism in beside it.
   *
   * What IS reused is the part worth reusing: how an artifact becomes a row, so a
   * result here and a result in the palette are the same row built by the same code.
   *
   * Inert on construction, like the two engines above — `fetchQuery` runs from
   * `search()`, and the only call site is gated on the artifacts scope.
   *
   * Content search is the next step, and it is this one request that changes.
   */
  const artifacts = useMemo(
    () =>
      createArtifactsProvider({
        fetchArtifacts: q =>
          queryClient.fetchQuery<ArtifactsResponse>({
            queryKey: ['artifacts', 'command-bar', 'name', q],
            queryFn: () => api.artifacts({ q: q || undefined }),
            staleTime: 15_000,
          }),
        openArtifact: slug => navigate(`/artifacts/${slug}`),
      }),
    [navigate, queryClient],
  )

  /**
   * The folders view's engine — this app's own folders corpus
   * (`./foldersProvider`), wired to THIS surface's seams.
   *
   * It lives beside this file rather than under the host palette's providers, and
   * that is the point: the host carries no Folders tab, so there is one
   * implementation of "find a folder and land on it" and the app owns it. The
   * corpus itself is hook-free precisely so the wiring stays here — React-Query for
   * the fetch, `usePaletteActions` for the route change. Every route change in this
   * overlay goes through that hook, and one component holding two navigation
   * mechanisms is how one of them ends up unexercised.
   *
   * Inert on construction, like the two engines above: the fetch runs from
   * `search()`, and the only call site is gated on the folders scope.
   */
  const folders = useMemo(
    () =>
      createFoldersProvider({
        fetchFolders: async () => {
          const rows = await queryClient.fetchQuery<ChatFolder[]>({
            // The SHARED key the sidebar and the filing path below read, so entering
            // the view on a warm cache costs nothing and a cold one pays once.
            queryKey: ['chat-folders'],
            queryFn: () => api.chatFolders(),
            staleTime: FOLDERS_STALE_MS,
          })
          // The key is shared, so what comes back is whatever the last writer put
          // there. A non-array reaches `folders.map` inside the ordering helper and
          // throws in render, which would take the whole launcher down over a bad
          // payload that only this one view needs.
          return Array.isArray(rows) ? rows : []
        },
        revealFolder: folderId => {
          // Store write BEFORE the route change: the request is held in the store
          // precisely because the sidebar may not be mounted yet, and its consuming
          // effect runs on mount as well as on change, so an early request is
          // replayed rather than dropped.
          dispatch(requestFolderReveal(folderId))
          navigate('/chat')
        },
        // The sidebar's own mode, so the listing is the sidebar's order and not a
        // second one. A dep of the memo: a mode switch rebuilds the engine, and the
        // scope query below keys on the mode too, so the switch is never served from
        // a 15-second-stale list in the old order.
        mode: folderSortMode,
      }),
    [dispatch, navigate, queryClient, folderSortMode],
  )

  /**
   * Which crewmates are working, keyed by crew NAME.
   *
   * Derived from the same live slot frames the attention group above reads, so the
   * running indicator costs this surface no request and cannot disagree with what the
   * Crewmates page shows — that page reads `running || subagents_running` off these
   * same frames.
   *
   * Keyed on `agent` rather than on the slot key, and that is sound because a member
   * slot is BORN with its crew pinned: the thread route sets `slot.agent` to the
   * member's exact name and refuses a slot whose agent is anything else. The slot key
   * would need the member's persisted `member_id` to reconstruct, which the catalog
   * rows do not carry.
   *
   * A mate with several member slots is working if ANY of them is, which is the
   * question the reader is asking: whether interrupting would land on a busy agent.
   */
  const runningMates = useMemo(() => {
    const byName = new Set<string>()
    for (const slot of liveSlots) {
      if (slot.mode !== 'member') continue
      if (!(slot.running || slot.subagents_running)) continue
      if (slot.agent) byName.add(slot.agent)
    }
    return byName
  }, [liveSlots])

  /**
   * The crewmates view's engine — this app's own roster corpus
   * (`./matesProvider`), wired to THIS surface's seams.
   *
   * Inert on construction, like the three engines above: `fetchQuery` runs from
   * `search()`, and the only call site is gated on the mates scope. That gating is
   * what keeps the root request-free, and it matters more here than for folders —
   * the catalog is not seeded into the cache by any WebSocket push, so there is no
   * warm-cache path to fall back on and an ungated read would be a real request on
   * every open.
   */
  const mates = useMemo(
    () =>
      createMatesProvider({
        fetchMates: async () => {
          const body = await queryClient.fetchQuery<{ agents?: KiroCrewAgent[] }>({
            // The SHARED key `NewCrewmateDialog` already uses for this exact read --
            // the sessionless `api.agentCatalog()`, whose answer is the GLOBAL roster.
            // A second key of this surface's own would be two caches of one response:
            // the dialog opens, fetches, and closes; the bar then refetches the same
            // bytes. (`['agents-installed']` is a DIFFERENT endpoint, `GET /api/agents`,
            // so it is not the one to join.)
            //
            // Sessionless on purpose: a session key would add whichever chat's project
            // templates are in scope, and this surface fires from anywhere in the
            // dashboard, so the crew a reader can reach must not depend on which pane
            // they last touched.
            queryKey: ['agents-catalog', 'global'],
            queryFn: () => api.agentCatalog(),
            staleTime: MATES_STALE_MS,
          })
          // The key is shared, so what comes back is whatever the last writer put
          // there. A non-array reaches `.filter` in the provider and throws in render,
          // which would take the whole launcher down over a payload only this view
          // needs.
          return Array.isArray(body?.agents) ? body.agents : []
        },
        openMate: name => navigate(mateRoute(name)),
        isRunning: name => runningMates.has(name),
      }),
    [navigate, queryClient, runningMates],
  )

  useEffect(() => {
    if (!open) return
    setQuery('')
    setDebounced('')
    setScope(null)
    setArgCommand(null)
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

  /**
   * Leave the argument state, revoking any activation started from it.
   *
   * Bumping `dialogRunRef` is the load-bearing half. The two effects above revoke on the
   * same principle they state — work is invalidated the moment the user walks away from
   * it — and stepping back OUT of the argument state is walking away just as much, only
   * at a narrower scope: the bar stays open. Without the bump, Enter on a slow session
   * create followed by Escape leaves that create in flight, and it resolves into a
   * seeded, auto-sent session the reader had already cancelled.
   *
   * One function rather than the increment repeated at each exit, because the failure
   * mode is an exit path that forgets it — which is exactly how this shipped: two revoke
   * sites existed and all three argument exits had none.
   */
  const exitArgumentState = useCallback(() => {
    dialogRunRef.current += 1
    setArgCommand(null)
    setActionError(null)
    setSelected(0)
    // Revoking is what makes the in-flight run stale, and a stale run no longer clears
    // its own guard, so the guard has to be released here or a revoked activation would
    // leave the bar permanently refusing the next Enter.
    setPendingRow(null)
  }, [])

  // A live view of the contributed commands for the ASYNC seeding path. The memo itself
  // is captured by value in that closure, so after an await it describes the apps as they
  // were when the row was activated -- which is the window this ref exists to close.
  const commandByIdRef = useRef<Map<string, ContributedCommand>>(new Map())

  // A failure describes the row the user just activated, so it must not outlive the
  // query that produced it. The in-flight guard is deliberately NOT cleared here:
  // typing while work is resolving must not re-arm a second activation of it.
  useEffect(() => {
    setActionError(null)
  }, [query])

  /**
   * Contributed commands by row id.
   *
   * The rows carry only what ranking needs; activation needs the prompt template,
   * the argument spec and the autoSend flag, so it resolves the row back to its
   * contribution here rather than the row model growing app-specific fields.
   */
  const commandById = useMemo(() => {
    const map = new Map<string, ContributedCommand>()
    for (const cmd of contributedCommands(apps ?? [])) map.set(cmd.id, cmd)
    return map
  }, [apps])
  commandByIdRef.current = commandById

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
    const lifted = new Set<string>()
    for (const slot of liveSlots) {
      const st = sessionStatus(slot, unreadSlots, slotStatusDetail[slot.key], simplifiedToolNames)
      if (st.style !== 'pill' || !st.label || !st.colorVar) continue
      lifted.add(slot.key)
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
          // Not awaited: the bar closes once this promise settles, and a session
          // open closes it at once -- a failed switch is explained by the pane's
          // own notice, not by keeping the bar up. The switch is handed on so the
          // composer is focused only once it has landed (#15732): `pending` enters
          // the slot synchronously, and a caret placed before the gateway answers
          // would file the keystrokes typed meanwhile under a slot a 404 then evicts.
          const switched = dispatch(switchSlot({ key: slot.key, announceOnMissing: true })).unwrap()
          navigate('/chat')
          focusComposerForOpenedSession(switched, slot.key, store)
        },
      })
    }
    // Then the sessions the reader was last in.
    //
    // This is the one thing the surface is opened for most and the one thing it used
    // to answer worst: switching back to yesterday's conversation meant entering the
    // sessions view and typing a name the reader had to remember. The facts are in
    // the same live store the block above reads, so the root pays no request for
    // them — which is the property that decides WHERE this can live. The full corpus
    // stays behind Search Sessions; this is the shortcut, not the index.
    //
    // Empty untitled slots are excluded: switching into a blank chat is what the New
    // Session command is for, and one of those rows is indistinguishable from
    // another. A slot already lifted into `attention` is excluded too — it is on
    // screen, above this, carrying more information than a second copy would.
    const recentSlots = liveSlots
      .filter(slot => !lifted.has(slot.key) && !isEmptyNewSlot(slot))
      .sort((a, b) => recencyEpoch(b) - recencyEpoch(a))
      .slice(0, RECENT_SESSION_ROWS)
    recentSlots.forEach(slot => {
      const st = sessionStatus(slot, unreadSlots, slotStatusDetail[slot.key], simplifiedToolNames)
      rows.push({
        id: `recent:${slot.key}`,
        title: slot.title || slot.key,
        group: 'recent',
        kind: 'invoke',
        icon: <MessageSquare size={14} className="lucide-inline" />,
        // Running state only, and never a pill: a pill means the session is waiting
        // on the reader, and every session that is has already been lifted into the
        // block above. Two treatments of "needs me" on one page is how the signal
        // stops meaning anything.
        status:
          st.style === 'dot' && st.label && st.colorVar
            ? { colorVar: st.colorVar, label: st.label, detail: st.detail, pulse: st.pulse }
            : undefined,
        // Pushed in recency order — recentSlots is sorted newest-first — which is the
        // order the root's idle-ordered `recent` group keeps, so the row needs no
        // sort key of its own.
        run: async () => {
          // See the attention row above (#15732).
          const switched = dispatch(switchSlot({ key: slot.key, announceOnMissing: true })).unwrap()
          navigate('/chat')
          focusComposerForOpenedSession(switched, slot.key, store)
        },
      })
    })
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
      {
        id: 'command:search-artifacts',
        title: i18nT('apps.commandBar.cmd_search_artifacts'),
        // "Artifacts" is this product's word, not the reader's: a first-time reader
        // called them "whatever they are". The subtitle names the things instead of
        // the category, which is what the settings rows in this same list do. The
        // sessions row above needs none, because "Sessions" already says it.
        subtitle: i18nT('apps.commandBar.cmd_search_artifacts_sub'),
        group: 'commands',
        kind: 'view',
        view: 'artifacts',
        // The glyph artifacts carry everywhere else in the product — the side
        // panel's view and the opened-artifact tab both use this one.
        icon: <Component size={14} className="lucide-inline" />,
        // `widget` earns its place: what the user saved is usually an mcwidget, and
        // that is the word they watched the chat call it. The rest are the nouns the
        // artifact list itself is filed under.
        keywords: ['widget', 'html', 'saved', 'document', 'chart'],
      },
      {
        // The sidebar's own folders, reached the way sessions and artifacts are: ONE
        // row that opens a view, not the folder list flattened into the first page.
        // A folder list is a corpus, and the reader has already learned from the two
        // rows above what a corpus costs them here — press Enter, then type.
        // Spreading tens of folder rows through the root instead made the same
        // collection behave unlike every other corpus this surface holds: demoted and
        // capped while the query was empty, so the feature read as missing, and
        // competing with commands once it was not.
        id: 'command:search-folders',
        title: i18nT('apps.commandBar.cmd_search_folders'),
        group: 'commands',
        kind: 'view',
        view: 'folders',
        icon: <Folder size={14} className="lucide-inline" />,
        keywords: ['sidebar', 'tree', 'group'],
      },
    )
    // The crew, reached the way the three corpora above are: ONE row that opens a
    // view. A roster is small enough that flattening it into the first page looks
    // tempting, and `matesProvider` records why it is not — the catalog behind it is a
    // FETCH, so a root group would either issue a request on every open of this bar or
    // show an empty Crewmates group on a cold install.
    //
    // Behind the PREVIEW GATE, because this row is an INGRESS to `/members` and that
    // page is registered with `previewFlag: PREVIEW_CREW`. Every other door to it
    // applies the same gate; a launcher row that did not would advertise a page the
    // operator has not opted into, and this app ships `defaultEnabled: true`, so it
    // would do so on a default install.
    if (crewPreview) {
      rows.push({
        id: 'command:search-mates',
        title: i18nT('apps.commandBar.cmd_search_mates'),
        // "Crewmate" is this product's word, the way "artifact" is: a reader who has
        // not opened the Crewmates page has no reason to know that the agents they
        // talk to have their own durable threads. The subtitle names what the view
        // gets them instead of restating the category.
        subtitle: i18nT('apps.commandBar.cmd_search_mates_sub'),
        group: 'commands',
        kind: 'view',
        view: 'mates',
        icon: matesIcon(),
        // The words a reader who has not learned "crewmate" would reach for. `agent`
        // earns its place above all of them: it is what the rest of the product calls
        // the thing answering them.
        keywords: ['crew', 'agent', 'member', 'mate', 'teammate', 'roster'],
      })
    }
    // Commands contributed by installed apps. This is the seam that lets a row live
    // outside this repository: the app declares the row and what it does, and the
    // host renders and runs it. Nothing app-authored executes here.
    for (const cmd of commandById.values()) {
      rows.push({
        id: cmd.id,
        title: cmd.title,
        // Falls back to the contributing app's name. A contributed row with no
        // subtitle is otherwise indistinguishable from a builtin one, and "which
        // app put this in my launcher" is the first thing a reader asks of a row
        // they did not recognise.
        subtitle: cmd.subtitle || cmd.appLabel,
        group: 'commands',
        kind: 'prompt',
        icon: contributedIcon(cmd.icon),
        keywords: cmd.keywords,
        // Derived, not declared: a command that needs an argument cannot act on an
        // empty query, so it has nothing to offer a launcher that has just opened.
        // Leaving this to the manifest would mean asking every app author to
        // volunteer their row out of the first page, which none would.
        idleDemote: cmd.argument !== null,
        // What the group's cap counts -- see `RootRow.contributed`. This is the one
        // place a row enters the `commands` group from outside the product, so it is
        // the one place the flag is set.
        contributed: true,
        appLabel: cmd.appLabel,
      })
    }
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
      if (!settingEntryOffered(entry, settingsGovernance)) continue
      rows.push({
        id: `${SETTING_ROW_PREFIX}${entry.id}`,
        // Matched as the other settings searches match it: label, synonyms, then
        // a description or tab that contains the query.
        match: query => settingRowMatch(query, entry),
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
  }, [apps, commandById, crewPreview, cycleTheme, dispatch, liveSlots, navigate, resolved, settingsGovernance, simplifiedToolNames, slotStatusDetail, store, unreadSlots])

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
  const {
    data: scopedResults,
    error: sessionsSearchError,
    isFetching,
    isError,
    refetch: refetchSessions,
  } = useQuery({
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

  /**
   * Artifacts view. ONE query for both of its states, unlike the sessions view
   * above.
   *
   * The sessions view needs two engines because its listing is a different thing
   * from its search — live slots and history buckets, not a query with no words in
   * it. Artifacts has no such split: the list endpoint with no `q` already answers
   * "the newest ones", which is exactly the listing, so an empty query is a
   * narrowing of zero rather than a second mode. Sending it through one query keeps
   * the two states on one cache key and removes the window where a view could show
   * a listing and a search at once.
   */
  const artifactsQuery =
    scope === 'artifacts' && debounced.trim().length >= ARTIFACTS_MIN_CHARS ? debounced.trim() : ''
  const {
    data: artifactRows,
    error: artifactsSearchError,
    isFetching: artifactsFetching,
    isError: artifactsError,
    refetch: refetchArtifacts,
  } = useQuery({
    // Distinct from the provider's own `['artifacts', 'command-bar', 'name', q]`
    // key, which caches the RESPONSE. This one caches the mapped rows. Both live
    // under `['artifacts']` so artifact mutations invalidate them, while the
    // `view` discriminator keeps their different response shapes on separate keys.
    queryKey: ['artifacts', 'command-bar', 'view', artifactsQuery],
    queryFn: () => artifacts.search(artifactsQuery) as Promise<Result[]>,
    enabled: scope === 'artifacts',
    staleTime: 15_000,
  })
  /**
   * The rows the view may render, capped — see {@link ARTIFACTS_ROW_LIMIT}.
   *
   * The LISTING state carries a group label, so the list says what it is. Without
   * one the view opened on five unexplained rows: a reader can guess they are the
   * recent ones, but nothing on screen said so, while every group in the root
   * announces itself with a header. A filtered list gets no header — what those
   * rows are is the word the reader just typed.
   */
  const artifactSlotRows = useMemo(
    () => (artifactRows ?? [])
      .slice(0, ARTIFACTS_ROW_LIMIT)
      .map(row => (artifactsQuery ? row : { ...row, groupLabel: i18nT('apps.commandBar.group_recent') })),
    [artifactRows, artifactsQuery],
  )

  /**
   * Folders view — the same two states the sessions view has, from ONE engine, for
   * the reason the artifacts view has one.
   *
   * The provider answers an empty query with every folder in the order the sidebar
   * draws them, so the listing the view lands on and the filtered list a query
   * produces are the same call with a different argument. There is deliberately no
   * {@link SESSIONS_MIN_CHARS} or {@link ARTIFACTS_MIN_CHARS} equivalent: the corpus
   * is the folder list already cached under `['chat-folders']`, so one character
   * costs a local filter rather than a round trip, and a view that refused to narrow
   * on one character would make the shortest names the hardest to reach. There is no
   * row cap either, for the same reason the sidebar draws every folder: the count is
   * the user's own filing, not a corpus that grows on its own.
   *
   * `enabled` on the scope is what keeps the root request-free. Entering the view is
   * the activation event that may pay for one folder fetch on a cold cache — the
   * same bargain the two views above make.
   */
  const folderQuery = scope === 'folders' ? debounced.trim() : ''
  const {
    data: folderRows,
    isFetching: foldersFetching,
    isError: foldersError,
    error: foldersSearchError,
    refetch: refetchFolders,
  } = useQuery({
    // Nested UNDER the corpus key, like the artifacts view above, because these rows
    // are DERIVED from `['chat-folders']` and every folder write ends in
    // `invalidateQueries({ queryKey: ['chat-folders'] })` (the create, delete and
    // update mutations in `pages/chat-sidebar/folders.ts`). React-Query matches that by key PREFIX, so a key outside
    // the corpus namespace is a cache no folder write can reach — and this view is
    // unmounted whenever the bar is closed, so its one chance to re-derive is the
    // remount, which refetches nothing that is still fresh. A folder deleted in that
    // window would otherwise stay listed and selectable for the whole `staleTime`,
    // pointing its reveal at an id the sidebar does not hold.
    //
    // The sort mode stays part of the identity: the same query in a different mode is
    // a different list, and without it a switch made in the sidebar would be served
    // from the previous order for the rest of the stale window.
    queryKey: ['chat-folders', 'command-bar', 'view', folderSortMode, folderQuery],
    queryFn: () => Promise.resolve(folders.search(folderQuery)) as Promise<Result[]>,
    enabled: scope === 'folders',
    staleTime: 15_000,
  })

  /**
   * Crewmates view — ONE engine for both of its states, like the artifacts and
   * folders views and for the same reason: the provider answers an empty query with
   * the whole roster, so the listing the view lands on and the filtered list a query
   * produces are the same call with a different argument.
   *
   * No minimum query length. The corpus is in hand once the catalog resolves, so one
   * character costs a local filter rather than a round trip — and a floor of two
   * would put the shortest crew names out of reach of their own first letter.
   *
   * No row cap either: the roster is the reader's own crew, not a corpus that grows
   * on its own, which is the argument the folders view makes about the sidebar tree.
   *
   * The running set is part of the KEY, not just a dependency of the engine memo.
   * These rows carry each mate's running state, read at the moment the provider
   * builds them — and rebuilding the engine does not re-run a resolved query, because
   * the key is what decides that. Without this the first frame to arrive after the
   * view opened would leave a "Working…" dot on a mate that had since finished, and a
   * mate that had just started would render idle.
   *
   * A SORTED name list rather than a count or a hash: two mates swapping which of
   * them is working leaves the count identical, and that is precisely the change that
   * has to reach the screen. Frames that change nothing about the roster's running
   * set — a token tick, another surface's slot — produce the same string and cost
   * nothing. The catalog fetch stays cached under its own key (MATES_STALE_MS), so a re-derivation
   * is a local re-map, not a request.
   */
  const mateQuery = scope === 'mates' ? debounced.trim() : ''
  const runningMatesKey = useMemo(() => [...runningMates].sort().join('\u0000'), [runningMates])
  const {
    data: mateRows,
    isFetching: matesFetching,
    isError: matesError,
    error: matesSearchError,
    refetch: refetchMates,
  } = useQuery({
    queryKey: ['command-bar', 'mates', mateQuery, runningMatesKey],
    queryFn: () => mates.search(mateQuery) as Promise<Result[]>,
    enabled: scope === 'mates',
    staleTime: 15_000,
    // The rows the reader is LOOKING AT, held across the key change the running set
    // causes. This view is the only one whose key moves on an event the reader did
    // not cause: a member slot finishing anywhere in the dashboard mints a new key,
    // and without this `mateRows` goes undefined, the list blanks to placeholders,
    // and the selection clamp below (`selected >= rowCount`) pulls the reader's
    // keyboard selection back to row 0 mid-typing. The re-derivation is a local
    // re-map off a cached catalog, so the stale frame it shows lasts an instant.
    //
    // HELD ONLY ACROSS THE RUNNING SET, never across the query. `prev => prev` held
    // them across both, and the second one is the defect the hold was never meant to
    // cover: once the catalog is past its stale window, the post-debounce key change
    // starts a real request, and rows answering the PREVIOUS words stayed on screen --
    // and actionable -- for as long as it took. Dropping them there is what the view
    // already does in every other state where it has no answer yet: it shows the
    // loading rows and Enter has nothing to act on.
    placeholderData: (prev, prevQuery) =>
      prevQuery?.queryKey?.[2] === mateQuery ? prev : undefined,
  })

  /**
   * WHAT THE RESULT ROWS ON SCREEN ANSWER, as a value the activation path can compare
   * against what the reader has actually typed.
   *
   * Every scoped view ranks from the DEBOUNCED query, because every one of them hits
   * the network; the root ranks from the live query and needs none of this. So for one
   * debounce interval after a keystroke, the rows in all four views answer the previous
   * query — and an Enter in that window acts on the row that was selected against it.
   * Typing `onc` and pressing Enter immediately opened `accountant`.
   *
   * The comparison is between what the view WOULD ask for the live query and what it
   * DID ask, rather than between the two query strings, because a keystroke that does
   * not change the request must not block anything. Sessions and artifacts only search
   * above a floor, so below it every query asks for the same listing: on the string
   * comparison, a one-character query in the artifacts view would have frozen Enter on
   * a listing that was the correct answer to it.
   *
   * ONE derivation for all four views. The first spelling of this guard read
   * `scope === 'mates'`, which fixed the view I was building and left the same defect
   * in its three siblings — a symptom-level fix, correctly refused as such.
   */
  const askedFor = useCallback(
    (q: string): string => {
      switch (scope) {
        // Below the floor the view shows recent sessions, which are the same rows for
        // every sub-floor query, so they answer all of them.
        case 'sessions':
          return q.length >= SESSIONS_MIN_CHARS ? q : ''
        case 'artifacts':
          return q.length >= ARTIFACTS_MIN_CHARS ? q : ''
        default:
          return q
      }
    },
    [scope],
  )

  /**
   * Whether the rows on screen are the rows for what the reader has typed.
   *
   * ONE condition, and only because the crewmates view's `placeholderData` above no
   * longer holds rows across a change of the query. Held-over rows that answer the same
   * words as the live query are not stale in the sense this guards: what moved was a
   * working dot, not what the row opens.
   */
  const rowsAnswerTheQuery = askedFor(debounced.trim()) === askedFor(query.trim())

  const use = useCallback((id: string) => setUsage(prev => recordUse(id, Date.now(), prev)), [])

  const enterScope = useCallback((view: Scope, keepQuery: string) => {
    setScope(view)
    setQuery(keepQuery)
    setDebounced(keepQuery)
    setSelected(0)
    inputRef.current?.focus()
  }, [])

  /**
   * Create a session, put `text` in it, and go there.
   *
   * ONE copy, shared by the ask row and the three bulk modes, because the ordering
   * here is the whole correctness of the thing and a second copy would be a second
   * place for it to rot: create without activating, take the claim, activate, seed,
   * navigate — and re-check the claim after every await.
   *
   * A NEW session, never the active one's composer: `ChatPage` consumes
   * `pendingInput` by REPLACING the slot's draft and persisting it, so seeding the
   * current slot would destroy a half-written message. These rows also fire from
   * anywhere in the dashboard, where the active session may be one the user last
   * touched hours ago.
   *
   * `autoSend` is what separates a question from a command. The ask row hands over
   * a sentence the user wrote and stops at a filled composer, so they can still
   * edit it. A bulk mode's text is not theirs to edit — it is generated from a row
   * they picked and a link they pasted, and the argument step was the deliberate
   * act — so it sends.
   *
   * The steps are run here rather than through the shared `newSessionWithToken`
   * because that helper is fire-and-forget: its failure path is a `console.error`,
   * so a gateway that refuses the create would leave the bar closing on nothing and
   * the user's text gone. Both callers carry something they cannot retype from
   * memory, so the bar closes only once the session exists, and a rejection keeps
   * it open with the field intact.
   */
  const seedNewSession = useCallback(
    (pendingKey: string, text: string, failureLabel: string, autoSend: boolean) => {
      const run = dialogRunRef.current
      const owned = () => dialogRunRef.current === run
      // Whether this seed belongs to a CONTRIBUTED command, decided before the awaits.
      // The Ask row uses this same path and is never in the map, so it is unaffected.
      const contributed = commandByIdRef.current.has(pendingKey)
      // The folder this session will be filed into, read BEFORE the awaits for the
      // same reason `contributed` is: the app can be disabled mid-flight, and the
      // filing below must not depend on the row still being in the map.
      const folderName = commandFolderName(commandByIdRef.current, pendingKey)
      // Still offered by an enabled app? `owned()` tracks the dialog's own lifetime and
      // cannot see this: the app can be disabled from the Apps page while the session
      // create is still in flight, which leaves the run legitimately owned and the
      // command gone. Checked after every await, because that is the window.
      const stillOffered = () => !contributed || commandByIdRef.current.has(pendingKey)
      setPendingRow(pendingKey)
      // `activate: false` is what makes the rest of this safe, and it exists for
      // exactly this shape: the thunk creates the session WITHOUT stealing focus so a
      // caller that must finish setting the slot up can do so before the user is able
      // to type into it. Leaning on "create makes the new slot active" is only true at
      // the instant it resolves -- and this callback can resolve long after the user
      // has moved on, at which point the seed lands in whatever they moved to.
      void dispatch(createSlot({
        activate: false,
        ...(contributed ? { memory_mode: 'persistent' } : {}),
      }))
        .unwrap()
        .then(
          async slot => {
            // The guard is released in `finally`, AFTER every await. Releasing it
            // earlier leaves it open across the awaits, so a second Enter during a slow
            // slot fetch starts a second create -- two sessions from one intent, which
            // is the exact failure the guard exists to prevent.
            try {
              if (!owned() || !stillOffered()) return
              // `keepTargetOnMissing`: this slot was JUST created, so a 404 from its
              // own detail fetch is a create/fetch race on a slot that does exist.
              // Without the opt-out, `switchSlot.rejected` treats the 404 as "target is
              // gone" and puts `activeSlot` back to where it came from (#6309) -- and
              // the seed below would then land in the reader's PREVIOUS conversation
              // and, with autoSend, fire there. A contributed prompt is typically an
              // instruction to act on a list of pull requests; running it against the
              // wrong session is the worst outcome this path has.
              //
              // With the opt-out the reducer keeps the fresh slot selected atomically,
              // so the rejection needs no repair from here and stays ignored: the
              // activation held either way.
              await dispatch(switchSlot({ key: slot.key, keepTargetOnMissing: true }))
                .unwrap()
                .catch(() => {})
              // Re-checked: the switch is another await, and the bar is still
              // dismissable across it. `stillOffered` too -- this is the last instant
              // before app-authored text becomes a message, and a disable that landed
              // during the switch must stop it here.
              if (!owned() || !stillOffered()) return
              dispatch(setPendingInput(text))
              // `autoSend=1` alone, never with `newSession=1`: the session already
              // exists -- we just created and activated it -- and asking ChatPage to
              // force a new one would land the text in a second, different session.
              navigate(autoSend ? '/chat?autoSend=1' : '/chat')
              onClose()
              // Filed LAST, and deliberately not awaited. A contributed row opens a new
              // session on every run, so unfiled they bury the reader's own chats and two
              // commands' runs interleave with nothing between them -- but the text is
              // already seeded by this point, so a slow, capped or refused folder API can
              // only cost this session its place in the sidebar. Contributed rows only:
              // the Ask row carries a sentence the reader wrote and belongs wherever they
              // are working, not in a folder named after a command.
              if (contributed && folderName) {
                void fileSessionInCommandFolder(
                  slot.key,
                  folderName,
                  Array.isArray(chatFoldersRef.current)
                    ? (chatFoldersRef.current as ChatFolderRow[])
                    : undefined,
                  // A folder this run created is not in the cache it just read, and the
                  // WebSocket push that would seed it is not guaranteed to arrive. Left
                  // uninvalidated, the sidebar can keep rendering a tree without the new
                  // folder and the next run reads the same stale list.
                  // Through the idle gate so the refetch cannot land inside a
                  // pending optimistic folder write in the sidebar.
                  () => invalidateFoldersWhenIdle(queryClient),
                )
              }
            } finally {
              // Only the OWNING run may clear the guard. Unconditionally, a stale
              // activation clears a LIVE one's: close and reopen during create A, start
              // create B, then let A resolve -- A's finally wipes B's `pendingRow`, and
              // the next Enter starts a second create for the same intent, which with
              // autoSend is a duplicate session that sends. `exitArgumentState` clears it
              // when it revokes, so a revoked run cannot leave the guard stuck either.
              if (owned()) setPendingRow(null)
            }
          },
          () => {
            if (!owned()) return
            setPendingRow(null)
            setActionError(i18nT('apps.commandBar.action_failed', { action: failureLabel }))
          },
        )
    },
    [dispatch, navigate, onClose, queryClient],
  )

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
      if (row.kind === 'prompt') {
        const cmd = commandById.get(row.id)
        // A row whose contribution is gone (the app was disabled while the bar was
        // open) must do nothing rather than fall through to the `invoke` branch and
        // silently close as if it had worked.
        if (!cmd) return
        if (!cmd.argument) {
          // Nothing to collect, so this is the whole action: seed and go.
          seedNewSession(cmd.id, cmd.prompt, cmd.title, cmd.autoSend)
          return
        }
        // No work yet -- this row's operation is defined by a value the user has not
        // given. The query is cleared because what they typed was the row's NAME, and
        // leaving it in a field that now means "paste the link" would read as a value
        // already supplied.
        setArgCommand(cmd)
        setQuery('')
        setDebounced('')
        setSelected(0)
        inputRef.current?.focus()
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
    [commandById, enterScope, navigate, onClose, pendingRow, seedNewSession, use],
  )

  const slots: Slot[] = useMemo(() => {
    // The argument state lists nothing: there is one thing to do and the field is
    // where it is done, so Enter belongs to the input rather than to a row. A
    // zero-row state is already part of this surface's keyboard contract -- it is
    // what moves the focus cue onto the field -- so this needs no new affordance.
    if (argCommand) return []
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
    if (scope === 'artifacts') {
      const out: Slot[] = artifactSlotRows.map(row => ({ key: row.id, tag: 'result' as const, row }))
      if (artifactsError) {
        out.push({ key: 'slot:retry', tag: 'retry-artifacts' })
      } else if (!artifactsFetching && out.length === 0 && artifactsQuery) {
        // A name that matched nothing still has somewhere to go — back to the
        // listing — so the view never bottoms out with nothing selectable. Gated on
        // there BEING a query: an instance with no artifacts at all has no listing
        // to return to, and offering one would be a row that does nothing.
        out.push({ key: 'slot:clear-query', tag: 'clear-query' })
      }
      return out
    }
    if (scope === 'folders') {
      const out: Slot[] = (folderRows ?? []).map(row => ({ key: row.id, tag: 'result' as const, row }))
      // The same two dead ends the views above have, told apart the same way: a
      // folder list that could not be read is a failure to retry, an empty result for
      // a query the user typed is a filter to drop. Both are ROWS so the keyboard can
      // reach them without leaving the list. The drop row is gated on there BEING a
      // query for the same reason it is above — a reader with no folders at all has no
      // listing to return to.
      if (foldersError) {
        out.push({ key: 'slot:retry', tag: 'retry-folders' })
      } else if (!foldersFetching && out.length === 0 && folderQuery) {
        out.push({ key: 'slot:clear-query', tag: 'clear-query-folders' })
      }
      return out
    }
    if (scope === 'mates') {
      const out: Slot[] = (mateRows ?? []).map(row => ({ key: row.id, tag: 'result' as const, row }))
      // The same two dead ends the views above have, told apart the same way. The
      // retry matters more here than anywhere else in this file: the catalog fetch is
      // the one this surface has no warm-cache path for, and `useAgents` records what
      // a swallowed failure on this exact endpoint cost — a failed load and a
      // one-crew install rendered identically, through six triage passes.
      if (matesError) {
        out.push({ key: 'slot:retry', tag: 'retry-mates' })
      } else if (!matesFetching && out.length === 0 && mateQuery) {
        out.push({ key: 'slot:clear-query', tag: 'clear-query-mates' })
      } else if (crewPreview && !matesFetching && out.length === 0 && mateRows !== undefined) {
        // NO CREW AT ALL, and the reader is one keystroke from fixing that. A centred
        // sentence naming the Crewmates page was the dead end here: a reader reported
        // that "nothing looks like a link", so the one thing they came to do had no
        // affordance. As a ROW it carries both halves the way `clear-query-mates` does
        // -- what is true, and what Enter does about it -- and the keyboard can reach it
        // without leaving the list.
        //
        // Gated on `mateRows !== undefined` so a cold fetch renders the skeleton rather
        // than briefly claiming the reader has no crew.
        out.push({ key: 'slot:open-crewmates', tag: 'open-crewmates' })
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
      // Typed text that is an ARTIFACT's name matched nothing above it: the root
      // ranks launcher rows on their own vocabulary, so "Q3 Revenue Chart" reaches
      // the artifacts view only by first typing a word like "artifact". This row is
      // the same way out the sessions corpus already had. Placed after it rather
      // than before so the row the sessions fallback has always occupied does not
      // move under a reader who navigates by position.
      out.push({ key: 'slot:fallback-artifacts', tag: 'fallback-artifacts' })
      // And the same way out for the folders corpus, for the same reason: the root
      // holds ONE row per corpus, so a typed folder name would otherwise reach
      // nothing until the reader thought to enter the view first and retype it. Last
      // of the three because a typed word is a session title or an artifact name far
      // more often than a folder name.
      out.push({ key: 'slot:fallback-folders', tag: 'fallback-folders' })
      // And the same way out for the crew, for the same reason: the root holds ONE row
      // per corpus, so a typed crew name reaches nothing until the reader thinks to
      // enter the view first and retype it. Last of the four, because a typed word is
      // a session title or an artifact name far more often than it is a crew's name —
      // and a crew name that IS typed is often distinctive enough to have already
      // matched the view row's own keywords above.
      // Behind the same preview gate as the view row: this row is a second, independent
      // way for a typed query to reach `/members`.
      if (crewPreview) out.push({ key: 'slot:fallback-mates', tag: 'fallback-mates' })
      // The recovery row exists for the dead end — a typed query that matched
      // nothing — not for every keystroke. Riding the fallback's own condition put a
      // row about switching the feature off under every successful search, and
      // ArrowUp from the top wrapped selection straight onto it.
      if (ranked.length === 0) out.push({ key: 'slot:recovery', tag: 'recovery' })
    }
    return out
  }, [argCommand, crewPreview, isError, isFetching, query, ranked, recentRows, scope, scopedResults, searchArmed, artifactSlotRows, artifactsError, artifactsFetching, artifactsQuery, folderQuery, folderRows, foldersError, foldersFetching, mateQuery, mateRows, matesError, matesFetching])

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
    rowCount === 0 &&
    ((scope === 'sessions' && (isFetching || (listingArmed && recentRows === undefined))) ||
      (scope === 'artifacts' && (artifactsFetching || artifactRows === undefined)) ||
      (scope === 'folders' && (foldersFetching || folderRows === undefined)) ||
      (scope === 'mates' && (matesFetching || mateRows === undefined)))

  /**
   * The scoped read for the LIVE query is still in flight.
   *
   * Distinct from {@link scopeLoading}, which is about an empty list: this is true even
   * while rows for the previous query are still on screen, which is the one frame a
   * latched Enter must not fire on — the debounced query may already match what was
   * typed while the rows answering it have not landed yet.
   */
  const scopeFetching =
    (scope === 'sessions' && isFetching) ||
    (scope === 'artifacts' && artifactsFetching) ||
    (scope === 'folders' && foldersFetching) ||
    (scope === 'mates' && matesFetching)

  useEffect(() => {
    if (selected >= rowCount) setSelected(Math.max(0, rowCount - 1))
  }, [rowCount, selected])

  /**
   * Activate the row at `index`.
   *
   * `via` says WHICH GESTURE asked, because one of the guards below is true of only one
   * of them. A pointer names its own target: the reader pressed the row they could read,
   * and that row opens what it says even if the list is about to be replaced. A keyboard
   * Enter names an INDEX, and an index means a different row the moment the rows change
   * under it -- which is the whole hazard the stale-row guard exists for.
   */
  const activateIndex = useCallback(
    (index: number, via: 'key' | 'pointer') => {
      const slot = slots[index]
      if (!slot) return
      // STALE ROWS MUST NOT ACT, whatever they are -- see `rowsAnswerTheQuery`.
      //
      // Above the switch rather than inside `case 'result':`, because the rows a scoped
      // view synthesizes are built from the same debounced query its results are, and
      // two of them do something WORSE than opening the wrong thing. `clear-query*`
      // wipes the query; `open-crewmates` navigates and closes the bar. Both sit at a
      // zero-result dead end, which is exactly where a reader types another character,
      // so a guard that covered results only discarded the text they had just typed --
      // and the stale row's own label still showed the old query, so nothing warned
      // them. The `root` has no scope, so this is inert there, which is correct: it
      // ranks from the live query and has nothing stale to act on.
      //
      // The window does not EAT the Enter, it holds it: a keyboard confirm in here is
      // latched and fired once the rows answer the query it was pressed against (see
      // `pendingEnter` and the effect that drains it). The confirmed query and the
      // selected row are captured here, so a later keystroke or arrow does not ride
      // the same latch into a row the reader never confirmed. An arrowed row's own
      // stable key is captured alongside its index, because a cached new query can
      // swap its rows in under the same index with no empty frame, and the drain fires
      // a non-top row only while its key still matches rather than whatever now sits at
      // that number.
      // Type-then-Enter is the
      // launcher's primary gesture, and dropping it left a fast typist pressing Enter
      // at a silent bar. A POINTER is never latched -- a click names the row it is on,
      // which opens what it says -- and it also drops any armed latch, so a click that
      // enters a scope without closing the bar cannot leave a keyboard confirm behind.
      if (via === 'pointer') setPendingEnter(null)
      if (via === 'key' && scope && !rowsAnswerTheQuery) {
        setPendingEnter({ query: query.trim(), index, key: slot.key })
        return
      }
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
        case 'fallback-artifacts':
          enterScope('artifacts', query)
          return
        case 'fallback-folders':
          enterScope('folders', query)
          return
        case 'fallback-mates':
          enterScope('mates', query)
          return
        case 'ask': {
          // Stops at a FILLED composer rather than sending: the user wrote this
          // sentence, so the last look at it is theirs.
          if (pendingRow) return
          seedNewSession(
            ASK_SLOT_KEY,
            query.trim(),
            i18nT('apps.commandBar.action_ask'),
            false,
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
        case 'retry-artifacts':
          void refetchArtifacts()
          return
        case 'retry-folders':
          void refetchFolders()
          return
        case 'retry-mates':
          void refetchMates()
          return
        case 'open-crewmates':
          // The page where a crewmate is made. No `?member=`: there is none to open.
          navigate('/members')
          onClose()
          return
        case 'clear-query':
        case 'clear-query-folders':
        case 'clear-query-mates':
          // Emptying the query is what re-arms the listing; the debounced copy has to
          // go with it or the view stays on the failed search for one more tick.
          setQuery('')
          setDebounced('')
          setSelected(0)
          inputRef.current?.focus()
          return
      }
    },
    [activateRoot, enterScope, navigate, onClose, pendingRow, query, refetchArtifacts, refetchFolders, refetchMates, refetchSessions, rowsAnswerTheQuery, scope, seedNewSession, slots],
  )

  /**
   * Fire a latched Enter once the rows answer the confirmed query, or drop it.
   *
   * The latch is set when a keyboard Enter lands in the debounce window (above), and
   * it carries the query the reader confirmed and the row they had selected. It drains
   * here rather than in the key handler because the thing it waits for -- the rows
   * catching up -- is an async settle, not a keystroke.
   *
   * It fires only when all of these still hold; otherwise it is dropped:
   *  - the reader has not typed on: the live query still equals the confirmed one.
   *    Typing another character confirms a different query, which this latch never
   *    stood for, so it is dropped rather than committed.
   *  - the selection has not moved: an arrow or a hover that moved `selected` after
   *    the Enter means the reader is aiming elsewhere, so the stale row is not fired.
   *  - the row at that index is still the one the reader confirmed, for a NON-top
   *    selection: row 0 is the primary gesture (open the best match for the typed
   *    query) and fires the live top row, but an arrowed row carries the key it was
   *    picked at, so a cached new query that swaps rows under the old indices drops
   *    the latch rather than open a stranger at that number.
   *  - the view has settled on rows that answer that query, and the selected row is a
   *    RESULT. A no-match drop row, an empty-roster row, or a failed-read retry wipes
   *    the query or navigates away, which is not what a reader who typed a name and
   *    pressed Enter asked for; the latch drops. Visible-stable, per the one
   *    constraint the reporter named.
   */
  useEffect(() => {
    if (!pendingEnter) return
    if (!scope) {
      setPendingEnter(null)
      return
    }
    // The reader typed on, or moved the selection: the live gesture differs from the
    // one that was confirmed.
    if (query.trim() !== pendingEnter.query || selected !== pendingEnter.index) {
      setPendingEnter(null)
      return
    }
    // Still settling, or the rows still answer an older query: hold.
    if (scopeLoading || scopeFetching || !rowsAnswerTheQuery) return
    const row = slots[pendingEnter.index]
    if (row?.tag !== 'result') {
      // Settled on a dead end: nothing the gesture meant to open.
      setPendingEnter(null)
      return
    }
    // Row 0 is the primary gesture — "open the best match for what I typed" — so it
    // fires the live top row even though its content swapped to answer the new query.
    // A NON-top index means the reader arrowed onto one specific row, so the row now
    // at that index must still be the one they picked: a cached new query swaps rows
    // under the old indices with no empty frame, and firing a changed row there opens
    // a stranger, the wrong-row hazard this latch exists to close.
    if (pendingEnter.index !== 0 && row.key !== pendingEnter.key) {
      setPendingEnter(null)
      return
    }
    setPendingEnter(null)
    activateIndex(pendingEnter.index, 'key')
  }, [pendingEnter, scope, query, selected, scopeLoading, scopeFetching, rowsAnswerTheQuery, slots, activateIndex])

  /**
   * Drop the latch the moment the reader leaves the window any other way.
   *
   * Escape out of the scope and Backspace out of it both land on `scope === null`;
   * closing the bar flips `open`. The drain effect above already refuses to fire
   * without a scope, but clearing here as well keeps a stale latch from riding a
   * reopen into the next visit.
   */
  useEffect(() => {
    if (!open || !scope) setPendingEnter(null)
  }, [open, scope])

  /**
   * Put the selected row's address on the clipboard.
   *
   * Returns whether the gesture was CLAIMED, so the key handler can decline ⌘C in the
   * one state where the bar has no row to answer with and let the browser's own copy
   * run instead.
   *
   * The address itself is resolved, never stored per row: `resolveCopyTarget` reads
   * what the row already says about where it points. That is the whole reason this is
   * a layer rather than a command -- a launcher that lists sessions, artifacts, pages
   * and settings can copy all four without any of the four knowing about copying, and
   * the next corpus added to the bar arrives copyable.
   */
  const copyTarget = useMemo(() => {
    const slot = slots[Math.min(selected, Math.max(0, slots.length - 1))]
    if (!slot) return null
    const row: CopyableRow | null =
      slot.tag === 'root' ? rootRowCopyable(slot.row) : slot.tag === 'result' ? slot.row : null
    if (!row) return null
    return resolveCopyTarget(row, {
      origin: window.location.origin,
      // The chat surface's own copy button builds the session link with this, and one
      // builder is the point: two would drift into two different links for one session.
      sessionLink: buildShareableUrl,
    })
  }, [selected, slots])

  const copySelected = useCallback(() => {
    if (slots.length === 0) return false
    if (!copyTarget) {
      // Claimed anyway, and answered. A launcher row is a thing the reader pressed a
      // key at, so the honest reply to "copy this" is that this one has no address --
      // silence would read as a copy that worked.
      setCopyError(null)
      setCopyNotice({ text: i18nT('apps.commandBar.copy_nothing') })
      return true
    }
    setCopyNotice(null)
    setCopyError(null)
    void copyToClipboard(copyTarget).then(ok => {
      if (!ok) {
        setCopyError({ text: i18nT('apps.commandBar.copy_failed'), what: copyTarget })
        return
      }
      // The address travels with the confirmation. "Copied" alone left the reader to
      // find out WHICH address on paste, and the two are genuinely different things: a
      // deployed artifact yields its public URL where every other row yields a link
      // into this dashboard.
      setCopyNotice({ text: i18nT('apps.commandBar.copied'), what: copyTarget })
    })
    return true
  }, [copyTarget, slots.length])

  // The outcome describes ONE row's copy, so it must not outlive the reader's attention
  // on that row: a "Copied" line still sitting there after they arrow somewhere else
  // reads as a claim about the row they arrived at.
  useEffect(() => {
    setCopyNotice(null)
    setCopyError(null)
  }, [selected, query, scope])

  /**
   * Enter in the argument state: check the value, then hand the command to a session.
   *
   * The check runs HERE, against the pattern the CONTRIBUTION declared, because the
   * collected text is spliced into an instruction handed to an agent with tools. A
   * command that writes somewhere must not be handed the last thing the reader
   * happened to copy, and the field they are still looking at is the cheapest place
   * in the system to refuse it. The app supplies the error message, since only the
   * app knows what shape it wanted.
   */
  // Re-measured on every change to what is previewed, since the same prompt clips or
  // does not depending on the value spliced into it -- AND on every change to the box it
  // is measured in. Content is not the only input: narrowing the viewport rewraps the
  // text, so a prompt that fitted starts clipping with the cue absent, which is the
  // unsafe direction (with autoSend, Enter then sends a tail the reader never saw).
  // A ResizeObserver rather than a window listener, because the box also moves when a
  // font finishes loading or the dialog reflows, and neither raises a resize event.
  useEffect(() => {
    const el = previewRef.current
    if (!el) {
      setPreviewClipped(false)
      return
    }
    const measure = () => setPreviewClipped(el.scrollHeight > el.clientHeight + 1)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [argCommand, query])

  const submitArgument = useCallback(() => {
    if (!argCommand || pendingRow) return
    // Re-resolved from the CURRENT contributions rather than trusting the snapshot
    // taken when the field opened. The field stays open across an arbitrary pause --
    // the reader is pasting a link -- and `apps` can change underneath it: the app can
    // be disabled or uninstalled from the Apps page in another tab, or by a gateway
    // event. The row vanishes from the list immediately, but this captured object
    // would not, so submitting would send the prompt of an app the reader had just
    // switched off. Re-resolving also picks up an edited prompt or a narrowed matcher
    // instead of acting on the version captured minutes ago.
    const live = commandById.get(argCommand.id)
    if (!live) {
      // Revokes too: the app is gone, so anything already in flight from this field
      // must not land either.
      exitArgumentState()
      setActionError(i18nT('apps.commandBar.argument_withdrawn'))
      return
    }
    if (!argumentIsValid(live, query)) {
      setActionError(live.argument?.patternError || i18nT('apps.commandBar.argument_invalid'))
      return
    }
    // The DISPLAYED command's matcher has to accept as well, not just the live one. The
    // preview is withheld until the value validates, so if the app broadened its matcher
    // while the field was open -- `url` with a host allowlist to `text`, say -- the
    // reader has been looking at a rejection the whole time and never saw a preview,
    // while the live matcher now passes. The prompt itself may be unchanged, so the
    // comparison below cannot catch it: what changed is whether anything was shown.
    if (!argumentIsValid(argCommand, query)) {
      setArgCommand(live)
      setActionError(i18nT('apps.commandBar.argument_changed'))
      return
    }
    // What was SHOWN has to be what is sent. Re-resolving above fixed a stale snapshot
    // firing after its app was disabled, but it introduced the mirror hazard: the
    // preview renders `argCommand`, so if the app's prompt or its autoSend changed while
    // the field was open, the reader would be consenting to text that is no longer the
    // text that goes out. Compared by resolved VALUE, not object identity -- the
    // contribution list is rebuilt on every apps refresh, so identity differs even when
    // nothing about the command did, and identity comparison would demand a second Enter
    // for no reason.
    //
    // On divergence the preview is refreshed and nothing is sent: the next Enter acts on
    // what is now on screen. Deliberately not a silent swap to the new prompt, which is
    // the whole finding, and deliberately not a refusal either -- the command is fine,
    // it just changed, and one keystroke re-consents.
    const shown = resolvePrompt(argCommand, query)
    const now = resolvePrompt(live, query)
    if (now !== shown || live.autoSend !== argCommand.autoSend) {
      setArgCommand(live)
      setActionError(i18nT('apps.commandBar.argument_changed'))
      return
    }
    setActionError(null)
    seedNewSession(live.id, now, live.title, live.autoSend)
  }, [argCommand, commandById, exitArgumentState, pendingRow, query, seedNewSession])

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
        // In the argument state Enter belongs to the FIELD, not to a row: there are
        // no rows, and what the user typed is the argument rather than a query.
        if (argCommand) {
          e.preventDefault()
          submitArgument()
          return
        }
        activateIndex(selected, 'key')
      } else if (
        (e.metaKey || e.ctrlKey) &&
        !e.altKey &&
        !e.shiftKey &&
        (e.key === 'c' || e.key === 'C')
      ) {
        // The launcher's copy gesture, on the chord every other application already
        // uses for copying, which is why it needs no affordance to teach.
        //
        // Declined in two states rather than claimed unconditionally. The input holds
        // focus the entire time the bar is open, so a reader who SELECTED part of what
        // they typed means that selection, and taking ⌘C from it would make this field
        // behave unlike every other text box on the machine. And the argument state
        // lists no rows at all -- what is on screen there is the value they are
        // pasting in, which is theirs for the same reason.
        const input = e.currentTarget
        const selecting = input.selectionStart !== null && input.selectionStart !== input.selectionEnd
        if (selecting || argCommand) return
        if (copySelected()) e.preventDefault()
      } else if (e.key === 'Backspace' && query === '' && (scope || argCommand)) {
        // Leaving a scope is Backspace on an empty input — the same gesture that
        // deletes a character, so it needs no separate key to learn. An argument
        // state leaves the same way: it is a place the user stepped into, and
        // abandoning the question must not also discard the whole bar.
        e.preventDefault()
        setScope(null)
        exitArgumentState()
      }
    },
    // No `onClose`: Escape belongs to the dialog below, so nothing in here
    // dismisses the bar. A dismissal reached from a row goes through
    // `activateIndex`, which is listed.
    [
      activateIndex,
      argCommand,
      copySelected,
      exitArgumentState,
      ime,
      query,
      rowCount,
      scope,
      selected,
      submitArgument,
    ],
  )

  if (!open) return null

  /**
   * The chip naming where the user is: a scope, or the mode asking for a link.
   *
   * One label for both states so the breadcrumb, its Escape handler and its
   * placeholder cannot disagree about which one is showing.
   */
  const navName = argCommand
    ? argCommand.title
    : scope === 'sessions'
      ? i18nT('apps.commandBar.cmd_search_sessions')
      : scope === 'artifacts'
        ? i18nT('apps.commandBar.cmd_search_artifacts')
        : scope === 'folders'
          ? i18nT('apps.commandBar.cmd_search_folders')
          : scope === 'mates'
            ? i18nT('apps.commandBar.cmd_search_mates')
            : ''
  /**
   * The failure this change is responsible for, and the only one that gets a notice.
   *
   * Each scope's retry ROW is pushed by that scope's own branch in the slot builder
   * above, so there is no shared failure flag to key this on: the notice belongs to
   * the artifacts scope alone.
   */
  const artifactsFailed = scope === 'artifacts' && !!artifactsError
  const foldersFailed = scope === 'folders' && !!foldersError
  const matesFailed = scope === 'mates' && !!matesError
  const searchError =
    scope === 'sessions'
      ? sessionsSearchError
      : scope === 'artifacts'
        ? artifactsSearchError
        : scope === 'folders'
          ? foldersSearchError
          : scope === 'mates'
            ? matesSearchError
            : undefined
  /**
   * What the failure SAYS, for the scope this change adds.
   *
   * ARTIFACTS AND FOLDERS. The sessions scope keeps the failure row it already had:
   * its text was the static `search_failed`, never a backend string, so nothing about
   * it misled the reader whose report motivated the wording here -- that reader met
   * this view. Reshaping it would have been this PR changing a surface it does not
   * own, on a symmetry argument.
   *
   * The concrete rejection is deliberately NOT rendered. Reading it off the error
   * yielded backend wording like "gateway unavailable", which a first-time reader
   * read as the "Run a local gateway" setting and then would not touch -- at the
   * moment of failure that is a cause they cannot interpret next to a fix they are
   * afraid of. Its structured journal report is resolved from the raw rejection
   * and passed directly to `ErrorNotice` below rather than recovered from the
   * friendly rendered sentence. That preserves the endpoint, status, and backend
   * code without exposing backend wording in visible text, a tooltip, or an aria
   * label.
   */
  const searchFailedText = artifactsFailed
    ? i18nT('apps.commandBar.artifact_search_failed')
    : foldersFailed || matesFailed
      ? i18nT('apps.commandBar.search_failed')
      : ''
  /**
   * How many name matches the cap is hiding.
   *
   * The endpoint returns every match, so this count is the real remainder rather
   * than a page boundary. Without it the cap was the one silent state left in the
   * view: a name matching thirty artifacts drew twelve rows and said nothing.
   */
  const artifactOverflow = Math.max(0, (artifactRows?.length ?? 0) - ARTIFACTS_ROW_LIMIT)
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
  ): { icon: ReactNode; title: ReactNode; subtitle?: ReactNode; accessory?: ReactNode; arrow?: boolean; dim?: boolean; wrap?: boolean } => {
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
                <span className="shrink-0 text-[11px] text-muted">
                  {kindLabel(row, query.trim().length > 0)}
                </span>
              )}
            </>
          ),
          arrow: row.kind === 'view' || row.kind === 'prompt',
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
      case 'fallback-artifacts':
        // Named with the query so the row states which text it carries, the same way
        // the sessions row above it does. Both are dim: they are ways out of the
        // root, not results, and a reader scanning for a match should read past them.
        //
        // Its subtitle is its OWN, not the view row's. It needs a gloss at all because
        // "artifact" is our word and a reader who arrives here by typing a name has
        // not necessarily read the view row. But when the typed word also matches the
        // command ("widget", "saved"), both rows are on screen at once, and sharing
        // one subtitle made them read as duplicates -- a reader could not tell how
        // their results would differ. So this one names what it does with the text
        // that is already typed, while the view row still describes the corpus.
        return {
          icon: <Package size={13} className="lucide-inline" />,
          title: i18nT('apps.commandBar.fallback_artifacts', { query }),
          subtitle: i18nT('apps.commandBar.fallback_artifacts_sub'),
          arrow: true,
          dim: true,
        }
      case 'fallback-folders':
        // Named with the query, dim, and arrowed like the two rows above it, for the
        // same reasons. No subtitle: "folder" is the reader's own word for the thing
        // the sidebar already shows them, so there is nothing to gloss.
        return {
          icon: <Folder size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.fallback_folders', { query }),
          arrow: true,
          dim: true,
        }
      case 'fallback-mates':
        // Named with the query, dim and arrowed like the three rows above it. No
        // subtitle, and for a different reason than the folders row: the view row this
        // leads to carries the gloss, and when the typed word matches both ("crew",
        // "agent") the two rows are on screen together — which is exactly where the
        // artifacts pair learned that one shared subtitle makes them read as
        // duplicates. This row names the text it carries; the view row describes the
        // corpus.
        return {
          icon: matesIcon(),
          title: i18nT('apps.commandBar.fallback_mates', { query }),
          arrow: true,
          dim: true,
        }
      case 'recovery':
        // Naming the corpora this surface does NOT reach -- at the moment the user is
        // looking for them -- is what keeps a typed knowledge or skill name from being
        // a silent dead end. Artifacts is no longer among them: it has a view of its
        // own now, so the string stopped listing it.
        //
        // The second clause names what CLICKING does, not what the user must switch
        // off, and names it without implying a transaction. Three readers in sequence
        // failed it. "disable Command Bar" described the end state, so the row itself
        // read as the thing that would disable their search box. Naming the App Store
        // fixed that and introduced a cost: "App Store sounds like it might want me to
        // install or buy something." The destination is neither -- it is AppDetailPage
        // at `/apps/detail/command-bar`, this app's OWN page under Apps, carrying its
        // Enabled switch. So the row names that section. Wording that blocks the users
        // a row exists for is not a smaller bug than a broken link.
        //
        // What is NOT solvable here: the same reader did not know what "Command Bar"
        // is. They are inside it. A row cannot teach the name of the app it lives in,
        // and the instruction is unactionable without naming the app to turn off.
        //
        // This is the one row that WRAPS. Every other row is a short label, but this
        // one is a sentence, and a sentence truncated mid-clause loses exactly the
        // half that explains itself, so the row is allowed two lines to finish. The
        // capture harness measures that: it asserts this row renders with no ellipsis
        // and that the clamp still engages when the text is longer than two lines.
        return {
          icon: <Package size={13} className="lucide-inline" />,
          title: i18nT('apps.commandBar.other_search_hint'),
          arrow: true,
          dim: true,
          wrap: true,
        }
      case 'retry-artifacts':
        // Separate tags keep the sessions row byte-identical to main, so this change
        // does not reshape a surface it does not own.
        return {
          icon: <RotateCcw size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.retry'),
        }
      case 'retry-folders':
        // Plain Retry, like the artifacts row above: WHAT failed is said by the
        // `ErrorNotice` above the list, which is where an error surfaced to the user
        // belongs (AUTOSDE `errors-use-error-notice`). Its own tag keeps the sessions
        // row below byte-identical to main, whose hand-written text this change does
        // not own and does not touch.
        return {
          icon: <RotateCcw size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.retry'),
        }
      case 'retry-mates':
        // Plain Retry, like the two rows above: WHAT failed is said by the
        // `ErrorNotice` over the list, which is where an error surfaced to the user
        // belongs (AUTOSDE `errors-use-error-notice`).
        return {
          icon: <RotateCcw size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.retry'),
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
          title:
            scope === 'artifacts'
              ? i18nT('apps.commandBar.no_artifact_match_show_recent', { query: artifactsQuery })
              : i18nT('apps.commandBar.no_match_show_recent', { query: scopedQuery }),
          dim: true,
        }
      case 'clear-query-folders':
        // The same row for the folders view, with the folder glyph rather than the
        // clock: what it returns to is the whole list in sidebar order, which has no
        // recency for a clock to stand for.
        return {
          icon: <Folder size={14} className="lucide-inline" />,
          title: i18nT('apps.commandBar.no_match_show_all_folders', { query: folderQuery }),
          dim: true,
        }
      case 'clear-query-mates':
        // The same row again, with the crew glyph: what it returns to is the whole
        // roster, which has no recency for a clock to stand for either.
        return {
          icon: matesIcon(),
          title: i18nT('apps.commandBar.no_match_show_all_mates', { query: mateQuery }),
          dim: true,
        }
      case 'open-crewmates':
        // Both halves in one row: there is no crew, and Enter goes where one is made.
        // Arrowed like every other row that leaves this surface, and WRAPPED, because
        // this one is a sentence and a sentence truncated mid-clause loses the half
        // that explains it.
        // Its own string, ending in what ENTER does -- the same shape
        // `no_match_show_all_mates` uses. The notice copy it replaced named an errand
        // somewhere else ("Add one on the Crewmates page") and said nothing about this
        // keystroke, so a crew-less reader -- the only audience this state has -- read it
        // as text and went hunting by hand.
        return {
          icon: matesIcon(),
          title: i18nT('apps.commandBar.no_mates_yet_open'),
          arrow: true,
          dim: true,
          wrap: true,
        }
    }
  }

  const renderSlot = (slot: Slot): ReactNode => {
    const parts = slotParts(slot)
    return (
      <>
        <span className="shrink-0 w-4 flex justify-center text-muted">{parts.icon}</span>
        <span className="flex-1 min-w-0">
          <span className={`block ${parts.wrap ? 'line-clamp-2' : 'truncate'}${parts.dim ? ' text-muted' : ''}`}>{parts.title}</span>
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
      className="fixed left-0 right-0 z-[9999] flex items-start justify-center bg-bg/60 backdrop-blur-xs animate-rise"
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
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- a dialog owns the keyboard dismissal of its own subtree; the handler below is Escape only, and every gesture inside is on a real control */}
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
          // An Escape mid-composition belongs to the IME — it cancels the candidate
          // list, not the search scope or the bar. The claim owns the whole decline
          // contract (a declined key keeps its default for the IME and is stopped
          // from leaking to outer layers), so it must run BEFORE the unconditional
          // preventDefault below takes the key for the dialog.
          if (!ime.claimKey(e)) return
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
          // Same for the argument state: the first Escape abandons the question, the
          // second closes the bar. A mode entered by mistake must not cost the user
          // the whole surface.
          if (argCommand) {
            exitArgumentState()
            inputRef.current?.focus()
            return
          }
          onClose()
        }}
      >
        <div className="flex items-center gap-2 px-3 py-2.5 border-b border-border">
          <Command size={15} className="lucide-inline text-muted shrink-0" />
          {(scope || argCommand) && (
            <>
              <button
                type="button"
                onClick={() => {
                  setScope(null)
                  exitArgumentState()
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
                className="shrink-0 max-w-[40%] truncate text-[13px] text-text bg-transparent border-none p-0 cursor-pointer focus-ring-accent rounded"
              >
                {navName}
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
            // Deliberately NO `maxLength`. It looked like defence in depth and was the
            // opposite: the browser clips a paste to the limit BEFORE `onChange`, so a
            // 2001-character value arrived as a valid-looking 2000-character prefix and
            // was sent -- exactly the silent truncation the module refuses to do, snuck in
            // one layer below the check that refuses it. The validator sees the whole
            // value and rejects it, which is what lets the reader be told.
            onChange={e => {
              setQuery(e.target.value)
              setSelected(0)
            }}
            {...ime.bindComposition()}
            onKeyDown={onKeyDown}
            placeholder={
              argCommand
                ? argCommand.argument?.placeholder || i18nT('apps.commandBar.placeholder_argument')
                : scope === 'artifacts'
                  ? i18nT('apps.commandBar.placeholder_artifacts')
                  : scope === 'folders'
                    ? i18nT('apps.commandBar.placeholder_folders')
                    : scope === 'mates'
                      ? i18nT('apps.commandBar.placeholder_mates')
                      : scope
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
            className={`flex-1 min-w-0 bg-transparent border-none outline-hidden rounded text-[13px] text-text placeholder:text-muted${
              rowCount === 0 ? ' focus-visible:ring-1 focus-visible:ring-border-strong' : ''
            }`}
          />
        </div>

        {/* No hand-off: the query typed into the bar above is unsaved — the
            navigation would close the bar and take it along. */}
        {actionError && (
          <div className="px-3 py-2 border-t border-border">
            <ErrorNotice message={actionError} variant="inline" />
          </div>
        )}

        {/* ARTIFACTS, FOLDERS AND CREWMATES: the sessions scope keeps the failure row
            it already had.
            The crewmates scope needs this MORE than the two beside it, and leaving it
            out was a real hole: its retry row deliberately carries no text because
            this notice is what names the failure, so without the disjunct a catalog
            rejection rendered one bare `Retry` and nothing else -- `findReport` never
            resolved, the endpoint, status and backend `code` were dropped, and a
            failed read was indistinguishable from a crew-less install. That is the
            exact pair of states `useAgents` records six triage passes being lost to.
            No hand-off: the combobox query is unsaved local state, and the
            hand-off navigation would unmount the command bar and discard it.
            Keep the notice outside the listbox so ErrorNotice can never put an
            interactive control inside an option; Retry remains a separate option
            on the combobox's Arrow/Enter path. */}
        {(artifactsFailed || foldersFailed || matesFailed) && (
          <div className="px-3 py-2 border-b border-border">
            <ErrorNotice
              message={searchFailedText}
              report={findReport(errMessage(searchError))}
              variant="inline"
            />
          </div>
        )}

        {/* FOLDERS: the shared settings read the order comes from has FAILED with no
            body to draw from, so the list below is the stored order whatever mode
            the person chose — said here the way the sidebar says it over its own
            tree (there is no sidebar on this surface), because a list drawn silently
            in the wrong order is the dead end, not the failure. The raw server
            string is the message so the notice's journal lookup still finds the
            endpoint and status -- under the title, smaller, because "config store"
            and "gateway" are the detail, not the lead; the plain line under it says
            what the list is showing (a picker: "listed", not the sidebar's
            "arrangement"), that nothing is asked of the reader (the shell's read
            retries on its own), and where the hand-off lives, since this notice has
            none: the query typed into the bar is unsaved and the navigation would
            take it along. */}
        {scope === 'folders' && folderSortError && (
          <div className="px-3 py-2 border-b border-border">
            <ErrorNotice
              variant="inline"
              className="flex-wrap"
              title={i18nT('pages.chatSidebar.folder_order_unavailable')}
              message={folderSortError}
              messagePlacement="below"
              testId="command-bar-folder-order-unavailable"
            />
            <p className="mt-0.5 text-[11px] text-muted" data-testid="command-bar-folder-order-unavailable-detail">
              {i18nT('pages.chatSidebar.folder_order_unavailable_detail_picker_ask')}
            </p>
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
            ) : argCommand ? (
              // The argument state's body. It has no rows by design, so this is not an
              // empty state to apologise for -- it is the question, and the app's own
              // hint says what answers it.
              //
              // The PROMPT PREVIEW is the consent mechanism for `autoSend`. A
              // contributed command sends app-authored text to an agent with tools as
              // if the reader had typed it; the reader picked the row and supplied the
              // value, but had no way to see the instruction itself. Showing the
              // resolved text — with the value already spliced in — is what makes the
              // next Enter informed rather than merely deliberate. It appears only
              // once the value satisfies the pattern, so it always shows what would
              // actually be sent, never a half-built template.
              <div role="status" className="px-3 py-4 text-[12px] text-muted space-y-2">
                <p className="text-text">{argCommand.subtitle || argCommand.appLabel}</p>
                {/* Attribution, not decoration. `subtitle` falls back to the app label, so
                    an app that writes its own subtitle used to erase the only mention of
                    who authored the prompt -- and this is the step where that prompt is
                    about to go to an agent with tools. Rendered whenever the subtitle
                    displaced it, so provenance is never the thing that got overwritten. */}
                {argCommand.subtitle && <p className="text-[11px]">{argCommand.appLabel}</p>}
                {argCommand.argument?.hint && <p>{argCommand.argument.hint}</p>}
                {argCommand.autoSend && argumentIsValid(argCommand, query) && query.trim() && (
                  <div className="pt-1 space-y-1">
                    <p className="text-[11px] uppercase tracking-wide" id="cb-will-send">
                      {i18nT('apps.commandBar.will_send')}
                    </p>
                    <pre
                      ref={previewRef}
                      // A NAMED region, not a bare focusable block. The cue below tells the
                      // reader to scroll this box, and a scroll container is not reachable
                      // from the keyboard on its own -- Safari never focuses one
                      // implicitly -- so without `tabIndex` the consent surface instructs a
                      // keyboard-only reader to read a tail they cannot reach, and then
                      // Enter sends it. `role`/`aria-labelledby` are what make the stop
                      // legitimate rather than a tab stop on inert text: it borrows the
                      // "Will send" heading already above it, so a screen reader announces
                      // what the region is and no catalog gains a string.
                      role="region"
                      aria-labelledby="cb-will-send"
                      // A SCROLL CONTAINER is the case the a11y rule below cannot see: the
                      // box clips a 4000-character prompt and the cue underneath tells the
                      // reader to scroll it, so the tab stop is what makes that instruction
                      // followable without a mouse. The rule's own allowlist is `tabpanel`
                      // only, so a labelled `region` cannot satisfy it; suppressed on this
                      // line rather than widened globally, and paired with the role and
                      // name above so the stop announces itself rather than being a silent
                      // halt on inert text.
                      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
                      tabIndex={0}
                      className="max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded border border-border bg-bg-hover/40 p-2 text-[11px] text-text focus-ring-accent"
                    >
                      {resolvePrompt(argCommand, query)}
                    </pre>
                    {previewClipped && (
                      // The box is the consent, so it must not let the reader believe
                      // they have read an instruction that continues out of sight. A
                      // prompt may run to 4000 characters and the unscrolled tail is
                      // exactly where a misleading manifest would put the part it does
                      // not want read. Measured rather than guessed from a line count,
                      // because wrapping decides what actually overflows.
                      <p className="text-[11px] text-warn">
                        {i18nT('apps.commandBar.will_send_clipped')}
                      </p>
                    )}
                  </div>
                )}
              </div>
            ) : (
              // Every other state now carries rows of its own, so this is the one
              // case left: a corpus that is genuinely empty.
              //
              // In the artifacts view that is the ONLY way to reach this branch, and
              // the copy depends on it: a query that matched nothing pushes the
              // clear-query row, a failure pushes the retry row, and a load in
              // flight renders the skeleton above. So an empty list here means the
              // user has saved nothing yet, and the string says that instead of
              // reporting a failed match against a query they never typed.
              //
              // The folders view reaches it the same way and for the same reason, so
              // it gets the same treatment: a reader with no folders is told they
              // have none, not that no sessions matched.
              //
              // The crewmates view has NO branch here, deliberately. Every empty case it
              // can reach pushes a ROW instead — `open-crewmates` for no crew,
              // `clear-query-mates` for a query that matched nothing, `retry-mates` for a
              // failed read — so this centred notice is unreachable for that scope, and a
              // branch for it would be a sentence with no affordance that nothing renders.
              <div role="status" className="px-3 py-6 text-center text-[12px] text-muted">
                {scope === 'artifacts'
                  ? i18nT('apps.commandBar.no_artifacts_yet')
                  : scope === 'folders'
                    ? i18nT('apps.commandBar.no_folders_yet')
                    : scope
                      ? i18nT('apps.commandBar.no_sessions')
                      : i18nT('apps.commandBar.no_matches')}
              </div>
            )
          ) : (
            slots.map((slot, i) => {
              const header = headerOf(slot, slots[i - 1], query.trim().length > 0)
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
                    onMouseDown={() => activateIndex(i, 'pointer')}
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

        {/* What the row cap is hiding. Outside the listbox on purpose: it is a fact
            about the list, not a row in it, and as an option it would be the one
            Enter did nothing to. Only in the artifacts view, where the cap exists. */}
        {scope === 'artifacts' && artifactOverflow > 0 && (
          <div className="px-3 py-1.5 border-t border-border text-[11px] text-muted">
            {i18nT('apps.commandBar.artifacts_more_keep_typing', { count: artifactOverflow })}
          </div>
        )}

        {/* What Enter does, named. The bar's promise is that Enter does something
            specific to the highlighted row, and nothing said what: the row carried
            its TYPE ("Command", "App") while the verb was left to be inferred from
            having pressed Enter before. Rendered only when a row exists, because
            with none there is no action to name. Kept to one line of muted text and
            a keycap — the panel's weight belongs on the selected row. */}
        {rowCount > 0 && (
          <div className="flex items-center justify-end gap-2 px-3 py-1.5 border-t border-border text-[11px] text-muted">
            {copyNotice ? (
              /* The copy outcome takes the footer's OWN line rather than a strip of its
                 own. The panel is height-capped, so any strip added anywhere shortens
                 the list: a row at the bottom vanished on every copy and a reader could
                 not tell whether it had scrolled away or been pushed out. One line in,
                 one line out, and nothing else moves. Affordable because the outcome is
                 transient -- it clears on the next keystroke, which is exactly when the
                 Enter hint starts mattering again.

                 `role="status"` on the WRAPPER, so the address is announced together
                 with the word: a reader who cannot see the line still learns which of
                 two possible addresses landed. */
              <span role="status" className="flex items-center gap-1.5 min-w-0 flex-1">
                <span className={`shrink-0 ${copyNotice.what ? 'text-accent' : 'text-warn'}`}>
                  {copyNotice.text}
                </span>
                {copyNotice.what && (
                  <span className="min-w-0 truncate" title={copyNotice.what}>
                    {copyNotice.what}
                  </span>
                )}
              </span>
            ) : (
              <>
                <span className="truncate">{actionLabel(slots[Math.min(selected, rowCount - 1)])}</span>
                <span className="shrink-0 px-1 rounded border border-border leading-4">
                  {ENTER_KEY}
                </span>
                {/* The copy chord, named only while the selected row HAS an address. A
                    gesture with no visible counterpart is one most readers never learn
                    exists, and this footer is already where the surface says what a key
                    does to the highlighted row. Withheld rather than greyed on a row
                    with no address: the hint doubles as the answer to "can this one be
                    copied", which a permanently-present label could not give. */}
                {copyTarget && (
                  <>
                    <span className="shrink-0">{i18nT('apps.commandBar.copy_hint')}</span>
                    <span className="shrink-0 px-1 rounded border border-border leading-4">
                      {COPY_KEY}
                    </span>
                  </>
                )}
              </>
            )}
          </div>
        )}
        {/* BELOW the footer, not above the list, and that placement is the point: a
            notice mounted over the rows pushed the whole list down on every copy, and a
            reader watching the row they had just copied saw it move and could not tell
            whether it had scrolled away or gone. At the bottom of the panel a copy
            changes nothing about where anything else sits.

            A clipboard write that did not land is an ERROR, so it goes through the
            product's error surface rather than the status line below it.
            No hand-off: the query typed into the bar is unsaved -- the navigation would
            close the bar and take it along. */}
        {copyError && (
          <div className="px-3 py-2 border-t border-border">
            <ErrorNotice message={copyError.text} variant="inline" />
            {/* The address the write did not land, in FULL and selectable. The message
                above names a remedy -- select the text and copy it yourself -- and on
                this path nothing else on screen holds that text. Not truncated, for the
                same reason: a reader who selects half an address gets half a link. */}
            <div
              className="mt-1 text-[12px] text-muted select-all"
              style={{ overflowWrap: 'anywhere' }}
            >
              {copyError.what}
            </div>
          </div>
        )}

        {/* The argument state has no row to name an action for, and it is the state
            that most needs one: the verb here is "approve" or "merge", and it fires on
            the next Enter. The spinner lives here for the same reason -- the work is
            attached to the field rather than to a row, so there is nowhere else for it
            to appear, and without it a slow create reads as a dead keypress. */}
        {rowCount === 0 && argCommand && (
          <div className="flex items-center justify-end gap-2 px-3 py-1.5 border-t border-border text-[11px] text-muted">
            {pendingRow ? (
              <>
                <Loader2 size={12} className="lucide-inline animate-spin shrink-0" />
                <span className="truncate">{i18nT('apps.commandBar.working')}</span>
              </>
            ) : (
              <>
                <span className="truncate">{argCommand.title}</span>
                <span className="shrink-0 px-1 rounded border border-border leading-4">{ENTER_KEY}</span>
              </>
            )}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
