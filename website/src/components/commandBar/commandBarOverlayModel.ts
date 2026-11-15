import type { CopyableRow } from '../../components/commandPalette/copyTarget'
import { scoreSettingEntry } from '../../components/commandPalette/settingsSearchCore'
import type { SettingEntry } from '../../components/commandPalette/settingsTypes'
import type { Result } from '../../components/commandPalette/types'
import { platformShortcut } from '../../utils/platform'
import { i18nT } from '../../i18n/t'

import type { OwnMatch, RankedRow, RootGroup, RootRow, RootRowKind } from './rootIndex'

/**
 * The Command Bar's list model: the scopes it can enter, every list position it can
 * offer (`Slot`), the limits each view keeps, and the pure labels a row, a section
 * header and the footer show. React-free, so the overlay and each of its owners read
 * one copy. A new slot tag, scope, limit or row label belongs here.
 */

/** Scoped views the bar can enter. Each one owns its own engine. */
export type Scope = null | 'sessions' | 'artifacts' | 'folders' | 'mates'

export const SESSIONS_MIN_CHARS = 2
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
export const ARTIFACTS_MIN_CHARS = 2
/**
 * Rows the artifacts view renders, listing and search alike.
 *
 * The endpoint returns the whole matching set with no page size, so this is the
 * only thing between a heavy instance and a thousand-row option list inside a
 * dialog. Sized to the list's own height rather than to the corpus: a name search
 * that needs more than this many rows to find its artifact needs a narrower query,
 * and the listing state is a shortcut to something recent, not an inventory.
 */
export const ARTIFACTS_ROW_LIMIT = 12
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
export const MATES_STALE_MS = 15_000
export const DEBOUNCE_MS = 150

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
export const RECENT_SESSION_ROWS = 3

/** Row id of the attention group's "+N more blocked" row. */
export const ATTENTION_MORE_ID = 'attention:more'

/**
 * A settings row's matcher: the settings scorer decides whether and how well it
 * matches, and its tier says which field to draw the hit on.
 */
export function settingRowMatch(query: string, entry: SettingEntry): OwnMatch | null {
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
export function kindLabel(
  row: { kind: RootRowKind; group: RootGroup; appLabel?: string },
  queryActive = false,
): string | null {
  // An attention row's column carries its LIVE STATE, which is both more useful than
  // a kind word and the reason that section exists. A session row reaches this group
  // only because its status is a pill, so the renderer always has one to show. The
  // group's `+N more blocked` overflow row is not a session and has no status, so its
  // column stays empty: its title already says what it is.
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
export function rootRowCopyable(row: RootRow): CopyableRow {
  return row.kind === 'navigate' && row.route
    ? { enter: { kind: 'navigate', route: row.route } }
    : {}
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
export type Slot =
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
export function actionLabel(slot: Slot): string {
  switch (slot.tag) {
    case 'root':
      if (slot.row.expand) return i18nT('apps.commandBar.action_show_all_blocked')
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
export function headerOf(slot: Slot, prev?: Slot, queryActive = false): string | null {
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
export const SKELETON_WIDTHS = ['58%', '46%', '34%'] as const

/**
 * The Enter keycap.
 *
 * A symbol rather than the word: the footer is a key hint, and every locale's
 * keyboard prints this glyph on the key itself. Held as a constant so the
 * untranslated-literal gate is not asked to judge a lone punctuation mark.
 */
export const ENTER_KEY = '\u21B5'
/**
 * The copy chord, spelled for the machine the reader is on.
 *
 * Through the product's own formatter rather than a literal, because the chord is not
 * the same everywhere: a hardcoded glyph would name a key Windows and Linux readers do
 * not have.
 */
export const COPY_KEY = platformShortcut('Cmd+C')

/**
 * Separator between a session row's folder and its timestamp.
 *
 * A constant for the same reason as the keycap: a lone middle dot is not copy, and
 * asking the untranslated-literal gate to judge one produces a false positive.
 */
export const META_SEP = ' \u00B7 '

/**
 * Key of the ask slot.
 *
 * A constant because three places have to agree on it: the slot itself, the
 * in-flight guard that refuses a second activation, and the spinner that says the
 * work is running. A literal repeated three times is how the spinner ends up
 * pointing at a row that is not the one working.
 */
export const ASK_SLOT_KEY = 'slot:ask'
