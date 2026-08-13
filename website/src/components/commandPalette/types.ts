import type { ReactNode } from 'react'

/**
 * A single result row produced by a {@link ResourceProvider} for the
 * Search Everywhere command palette.
 *
 * Highlighting note (`frontend-security` lint rule): `indices` are positions
 * into `title` that matched the query. The palette renders the highlight by
 * splitting `title` on those indices and emitting `<mark>` / `<strong>` as
 * React nodes keyed off the indices — never by building an HTML string.
 */
export interface Result {
  /** Stable unique id within a result set, usually `${providerId}:${entityId}`. */
  id: string
  /** Id of the {@link ResourceProvider} that produced this result. */
  providerId: string
  /** Primary display label. */
  title: string
  /** Optional secondary line (path, snippet, or metadata). */
  subtitle?: string
  /** Indices into `subtitle` that matched the query (for highlighting a
   * content-match snippet, e.g. a session body hit). */
  subtitleIndices?: number[]
  /**
   * Rendered icon node. By convention a `lucide-react` element with
   * `className="lucide-inline"` (`use-lucide-icons` lint rule); never inline
   * `<svg>` or an emoji.
   */
  icon: ReactNode
  /** Fuzzy-match score; higher is a better match. Used to rank results. */
  score: number
  /** Indices into `title` that matched the query, for highlight rendering. */
  indices: number[]
  /**
   * Command-palette recents grouping (Current / Planned / Older day-buckets).
   * When set, the palette's default (empty-query) view inserts a section
   * header whenever this value changes between consecutive results. Ignored by
   * the search views.
   */
  groupLabel?: string
  /** De-emphasize the row (archived / older sessions in the recents view). */
  faded?: boolean
  /** Live status indicator dot for the current-sessions group.
   * `colorVar` is a CSS custom property name (e.g. "--warn"). */
  statusDot?: { colorVar: string; pulse?: boolean }
  /** Inline status line. `statusStyle` picks the treatment:
   *  - 'pill' → a filled pill (bg = statusColorVar, light text) + muted detail
   *    (e.g. "Needs input" / "Approve").
   *  - 'dot' → a colored dot + colored label + muted detail (e.g. "Thinking…"),
   *    matching the chat sidebar.
   * `statusColorVar` is a CSS var name (e.g. "--warn", "--accent", "--info"). */
  statusStyle?: 'pill' | 'dot'
  statusColorVar?: string
  statusPulse?: boolean
  statusLabel?: string
  statusDetail?: string
  /** Right-aligned relative timestamp (recents view), e.g. "09:46" or
   * "Yesterday 21:12". Mirrors the sidebar's time column. */
  timestamp?: string
  /** True when the session is pinned (recents view) — pins sort first and get
   * a marker, matching the sidebar. */
  pinned?: boolean
  /** Folder name the session is filed under (recents view), shown as a chip. */
  folder?: string
  /** True for a new/empty session row — rendered with a "+" marker. */
  isNew?: boolean
  /**
   * Declarative description of what Enter / ⌘Enter / ⌥Enter should do for this
   * result, per the §2 Enter matrix. This is the typed contract a
   * central {@link OnEnter} dispatcher switches on — it replaces the three
   * opaque `on*Activate` closures below as the source of truth.
   *
   * Optional during the migration: providers that have not yet been ported to
   * the declarative model still rely on `onActivate` / `onCmdActivate` /
   * `onAltActivate`. New / ported providers SHOULD populate `enter`; the
   * dispatcher prefers it when present and falls back to the closures
   * otherwise.
   */
  enter?: EnterAction
  /** Primary activation (Enter). Context-aware per the §2 Enter matrix. */
  onActivate: () => void
  /** ⌘Enter / Ctrl+Enter activation (always-new-session semantics). */
  onCmdActivate?: () => void
  /** ⌥Enter / Alt+Enter activation (read / preview, e.g. open SKILL.md). */
  onAltActivate?: () => void
}

/**
 * A source of results for the command palette. Each provider also backs one
 * tab in the palette tab strip (keyed by {@link ResourceProvider.id}).
 *
 * `search` may be synchronous or asynchronous; the aggregator and palette
 * await the result either way.
 */
export interface ResourceProvider {
  /** Unique provider id; also used as the tab key. */
  id: string
  /** Human-readable tab label. */
  label: string
  /**
   * Tab icon node. By convention a `lucide-react` element with
   * `className="lucide-inline"` (`use-lucide-icons` lint rule).
   */
  icon: ReactNode
  /** Run the search for a query. May return a Promise or a value. */
  search(query: string): Promise<Result[]> | Result[]
  /**
   * Shortest non-empty query this provider can actually answer. A provider
   * whose backend (or short-circuit) returns nothing below N declares N here,
   * so the palette can render a "keep typing" empty state instead of the
   * misleading generic "No matches" for a query the provider never searched.
   * Absent means no minimum. An EMPTY query is exempt by contract (it is the
   * recents / scoped-listing view, not a search). Declared values must be
   * >= 2: a minimum of 1 is indistinguishable from "no minimum" (the empty
   * query is already exempt), and the copy key interpolates the number
   * without plural forms, so 1 would also render "1 characters".
   */
  minQueryChars?: number
}


/**
 * Declarative Enter-action contract for a {@link Result} (§2).
 *
 * A discriminated union (on `kind`) that captures *what* should happen on
 * Enter / ⌘Enter / ⌥Enter for a result, as data rather than as opaque
 * closures. A single central dispatcher ({@link OnEnter}, implemented in
 * `CommandPalette.tsx`) switches on `kind` and applies the modifier branch.
 * Decoupling intent (this type) from execution (the dispatcher) is what makes
 * the Enter matrix unit-testable per result type.
 *
 * The locked per-type matrix:
 *
 * | kind            | Enter (primary)                         | ⌘Enter (withModifier)            | ⌥Enter (preview)        |
 * |-----------------|-----------------------------------------|----------------------------------|-------------------------|
 * | `open-session`  | open / switch to the session            | open in a split pane (Grid)      | —                       |
 * | `insert-token`  | insert the token into the active chat   | new session seeded with token    | open source (e.g. SKILL.md) |
 * |  (skill/prompt) |  composer; if no active chat → new      |                                  |                         |
 * |                 |  session seeded with the token          |                                  |                         |
 * | `open-knowledge`| open / navigate to the entry            | attach the entry as chat context | preview the entry       |
 * | `navigate`      | navigate to the route                   | (same as Enter)                  | —                       |
 * | `invoke`        | run the action callback                 | (same as Enter)                  | —                       |
 *
 * Payloads use primitive fields (ids + titles) rather than the provider-level
 * ref types (`SessionRef`, `KnowledgeRef`) so this module stays free of any
 * `providers/*` import — providers depend on `types.ts`, never the reverse.
 */
export type EnterAction =
  /** Sessions tab. Enter opens/switches; ⌘Enter opens in a split pane. */
  | { kind: 'open-session'; sessionKey: string; title: string }
  /**
   * Skills / Prompts tabs. Enter inserts `token` ("$skill" / "@prompt") into
   * the active chat composer, or — when no chat is active — opens a new session
   * seeded with the token. ⌘Enter always opens a new seeded session. `source`
   * (e.g. a `SKILL.md` path), when present, backs ⌥Enter preview.
   */
  | { kind: 'insert-token'; token: string; tokenKind: 'skill' | 'prompt'; source?: string }
  /**
   * Knowledge tab. Enter opens / navigates to the entry; ⌘Enter attaches it as
   * context to the active chat.
   */
  | { kind: 'open-knowledge'; entryId: string; title: string }
  /** Pages tab. Enter navigates to a client route. No distinct modifier action. */
  | { kind: 'navigate'; route: string }
  /** Actions tab. Enter invokes the free action callback. No distinct modifier action. */
  | { kind: 'invoke'; run: () => void }

/**
 * Discriminant strings of {@link EnterAction}, for exhaustiveness checks and
 * test fixtures.
 */
export type EnterActionKind = EnterAction['kind']

/**
 * Central Enter dispatcher signature (§2). `CommandPalette.tsx`
 * implements one `onEnter(result, withModifier)` that reads `result.enter`
 * (falling back to the legacy `on*Activate` closures while providers are
 * migrated) and routes on the action `kind`:
 *
 *  - `withModifier === false` → primary Enter behavior.
 *  - `withModifier === true`  → ⌘/Ctrl+Enter behavior (always-new-session /
 *    attach-as-context per the matrix above).
 *
 * ⌥/Alt+Enter (preview) stays a separate, non-closing path and is intentionally
 * NOT folded into this two-argument signature.
 */
export type OnEnter = (result: Result, withModifier: boolean) => void
