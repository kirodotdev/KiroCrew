/**
 * The chat side panel's "+" menu: the catalog of views it can open, and which
 * of them a given panel offers.
 *
 * The label and description catalog keys (`NEW_MENU_LABEL_KEY`,
 * `NEW_MENU_DESC_KEY`), the three row groups (`NEW_MENU_GROUPS`), the
 * Developer Mode gate (`DEV_ONLY_VIEWS`), `newMenuSections` (the groups after
 * every gate: Terminal, Developer Mode, Summary, the host's withdrawals and the
 * pinned views) with the `SidePanelWithholdable` type a host withdraws with,
 * and the shared menu row `AddMenuItem`. `SidePanel.tsx` renders the menu and
 * the empty-state launcher from these and re-exports the public names, which
 * callers import from there. This module never imports `SidePanel.tsx`.
 */
import { forwardRef, type ComponentPropsWithoutRef, type ElementRef, type ReactNode } from 'react'
import { Bot, Workflow, ScrollText, MessageCircleQuestionMark, TerminalSquare, GitPullRequest, GitBranch, History, Component, Globe, CircleDot, Folders, Link as LinkIcon, PanelRight, Layers, ListTree, Pin } from 'lucide-react'
import type { ViewKind } from '../../hooks/usePanelTabs'
import { PINNED_VIEWS } from '../../hooks/usePanelTabs'
import { DropdownMenuItem } from '../../components/ui/dropdown-menu'

/**
 * Catalog KEYS for the + menu's labels and one-line descriptions.
 *
 * Keys, not strings, and in their own tables rather than as `NEW_MENU` fields:
 * this module evaluates once at import, so an `i18nT()` call here would freeze
 * the boot language and never re-resolve on a language switch (see
 * `lib/effort.ts`). The lookups happen at the two render sites in `SidePanel.tsx`.
 *
 * Flat `Record`s of full literal keys, indexed inline at the `i18nT()` call,
 * because that is the form `scripts/check-i18n-keys.mjs` can resolve statically
 * — `i18nT(item.labelKey)` over a loop variable cannot be resolved, so a field
 * on `NEW_MENU` would have made every menu key unverifiable.
 *
 * Keyed by `ViewKind | 'terminal'` (not `string`) so adding a view without its
 * label and description is a type error rather than a missing-key render.
 */
export const NEW_MENU_LABEL_KEY: Record<ViewKind | 'terminal', string> = {
  'command-center': 'commandCenter.title',
  changes: 'pages.chat.sidePanel.menu_changes',
  issues: 'pages.chat.sidePanel.menu_issues',
  files: 'pages.chat.sidePanel.menu_files',
  links: 'pages.chat.sidePanel.menu_links',
  artifacts: 'pages.chat.sidePanel.menu_artifacts',
  subagents: 'pages.chat.sidePanel.menu_subagents',
  workflows: 'pages.chat.sidePanel.menu_workflows',
  logs: 'pages.chat.sidePanel.menu_logs',
  crewlog: 'pages.chat.sidePanel.menu_crewlog',
  context: 'pages.chat.sidePanel.menu_context',
  side: 'pages.chat.sidePanel.menu_side',
  browser: 'pages.chat.sidePanel.menu_browser',
  terminal: 'pages.chat.sidePanel.menu_terminal',
  git: 'pages.chat.sidePanel.menu_git',
  summary: 'pages.chat.sidePanel.menu_summary',
  pins: 'pages.chat.sidePanel.menu_pins',
}

export const NEW_MENU_DESC_KEY: Record<ViewKind | 'terminal', string> = {
  'command-center': 'commandCenter.description',
  changes: 'pages.chat.sidePanel.menu_changes_desc',
  issues: 'pages.chat.sidePanel.menu_issues_desc',
  files: 'pages.chat.sidePanel.menu_files_desc',
  links: 'pages.chat.sidePanel.menu_links_desc',
  artifacts: 'pages.chat.sidePanel.menu_artifacts_desc',
  subagents: 'pages.chat.sidePanel.menu_subagents_desc',
  workflows: 'pages.chat.sidePanel.menu_workflows_desc',
  logs: 'pages.chat.sidePanel.menu_logs_desc',
  crewlog: 'pages.chat.sidePanel.menu_crewlog_desc',
  context: 'pages.chat.sidePanel.menu_context_desc',
  side: 'pages.chat.sidePanel.menu_side_desc',
  browser: 'pages.chat.sidePanel.menu_browser_desc',
  terminal: 'pages.chat.sidePanel.menu_terminal_desc',
  git: 'pages.chat.sidePanel.menu_git_desc',
  summary: 'pages.chat.sidePanel.menu_summary_desc',
  pins: 'pages.chat.sidePanel.menu_pins_desc',
}

/** Views offered by the + menu, in the three semantic groups the menu renders
 *  with a separator between them. `kind` is the PERSISTED tab id
 *  (`usePanelTabs`), so it stays a code constant — only its label and
 *  description are localised.
 *
 *  Groups, not one flat list, because the eight rows were three unrelated
 *  kinds of thing in arbitrary order: what this chat produced, surfaces the
 *  user drives themselves, and diagnostics. They are deliberately UNLABELLED
 *  (rules only): three group headings would add ~90px of chrome to an
 *  eight-row menu for hierarchy the grouping already conveys.
 *
 *  Each group carries a stable `id`. It is never rendered — it exists to be the
 *  group's React key. Neither the group's index nor its contents can serve:
 *  gating rows changes the contents and dropping an emptied group shifts the
 *  indices, and either shift remounts a group mid-interaction, detaching the
 *  menu item the user is clicking. The id is fixed at declaration, so a gate
 *  flipping only re-renders rows within a group that keeps its identity.
 *
 *  Every key of `NEW_MENU_LABEL_KEY` must appear exactly once across the
 *  groups — `sidePanelAddMenu.test.tsx` pins that partition, so adding a view
 *  without placing it in a group fails rather than silently dropping it. */
const NEW_MENU_GROUPS: { id: string; items: { kind: ViewKind | 'terminal'; icon: ReactNode }[] }[] = [
  // Session output — what this chat referenced or produced. (Changes / Files /
  // Artifacts are auto-pinned and filtered out below; they are listed here so
  // this table stays the complete catalog of views.)
  {
    id: 'session-output',
    items: [
      { kind: 'command-center', icon: <PanelRight size={15} /> },
      { kind: 'summary', icon: <ListTree size={15} /> },
      { kind: 'pins', icon: <Pin size={15} /> },
      { kind: 'changes', icon: <GitPullRequest size={15} /> },
      { kind: 'issues', icon: <CircleDot size={15} /> },
      { kind: 'files', icon: <Folders size={15} /> },
      { kind: 'links', icon: <LinkIcon size={15} /> },
      { kind: 'artifacts', icon: <Component size={15} /> },
      { kind: 'subagents', icon: <Bot size={15} /> },
      { kind: 'workflows', icon: <Workflow size={15} /> },
      { kind: 'git', icon: <GitBranch size={15} /> },
    ],
  },
  // Interactive workspaces — the surfaces the user types into. Terminal is a
  // per-chat shell: its tab lives in this chat's panel state, so it comes and
  // goes with the session, unlike the app-wide dock terminal in the nav rail.
  {
    id: 'workspaces',
    items: [
      { kind: 'side', icon: <MessageCircleQuestionMark size={15} /> },
      { kind: 'browser', icon: <Globe size={15} /> },
      { kind: 'terminal', icon: <TerminalSquare size={15} /> },
    ],
  },
  // Diagnostics.
  {
    id: 'diagnostics',
    items: [
      { kind: 'logs', icon: <ScrollText size={15} /> },
      { kind: 'context', icon: <Layers size={15} /> },
      { kind: 'crewlog', icon: <History size={15} /> },
    ],
  },
]

/** One row of the "+" menu's built-in groups: the shared menu item with the
 *  roomier gap and padding those rows use. Forwards every other prop (the
 *  `uiLocation` marker included) to the primitive. */
const AddMenuItem = forwardRef<ElementRef<typeof DropdownMenuItem>, ComponentPropsWithoutRef<typeof DropdownMenuItem>>(
  function AddMenuItem(props, ref) {
    return <DropdownMenuItem {...props} ref={ref} className="gap-2.5 py-2" />
  },
)

/** Views behind the Developer Mode consent gate (Settings > Developer) — the
 *  same gate the standalone Developer page uses. All three are raw
 *  instrumentation of the agent's own execution (the session's tool-call log,
 *  the context window's composition, and the folds over the session's crew log)
 *  rather than anything the session produced, so none belongs in a
 *  non-developer's menu. Gating all of them empties the diagnostics group
 *  outright when Developer Mode is off — which is exactly the empty-group case
 *  `newMenuSections` drops. */
const DEV_ONLY_VIEWS = new Set<ViewKind | 'terminal'>(['logs', 'context', 'crewlog'])

/** Which `+`-menu entries are offered, given the gates that hide entries:
 *  Terminal is hidden when the feature is disabled server-side, the
 *  diagnostics views (Logs, Context breakdown) are hidden unless Developer
 *  Mode is on, and **Summary is hidden while session summaries are disabled**.
 *  The permanently pinned views (Changes / Files / Artifacts) are never listed;
 *  they are always present in the strip.
 *
 *  Summary is gated because the feature is opt-in and its settings toggle ships
 *  separately: advertising the entry while `session_summary.enabled` is false
 *  sends every reader to a panel that explains it is off and offers no way to
 *  change that. Hiding the row is the only option that removes the dead end
 *  rather than wording around it, and it reverses itself the moment the flag
 *  flips.
 *
 *  Grouped, and **emptied groups are dropped**: with Developer Mode off the
 *  whole diagnostics group disappears, and Terminal disabled shrinks Workspaces
 *  to two rows. A group that filtered down to nothing would otherwise render
 *  as a separator with no rows after it.
 *
 *  `hiddenViews` is the HOST's withdrawal: views whose data this host cannot
 *  feed (the Members page has no transcript link index, so Issues / Links /
 *  Pins would render an affirmative "none" that is false), and Terminal while
 *  the host's slot is not yet confirmed (a PTY opened into the provisional
 *  bucket would be orphaned by the re-key). Withheld here and from the pinned
 *  block alike — an empty view is worse than no view. */
export function newMenuSections(
  opts: { devMode: boolean; terminalEnabled: boolean; summaryEnabled: boolean; hiddenViews?: ReadonlySet<SidePanelWithholdable> },
): { id: string; items: { kind: ViewKind | 'terminal'; icon: ReactNode }[] }[] {
  return NEW_MENU_GROUPS
    .map(group => ({
      id: group.id,
      items: group.items.filter(item =>
        (opts.terminalEnabled || item.kind !== 'terminal')
        && (opts.devMode || !DEV_ONLY_VIEWS.has(item.kind))
        && (opts.summaryEnabled || item.kind !== 'summary')
        && !opts.hiddenViews?.has(item.kind)
        && !(PINNED_VIEWS as string[]).includes(item.kind),
      ),
    }))
    .filter(group => group.items.length > 0)
}

/** What a host may withdraw through `hiddenViews`: a chat view, the per-chat
 *  Terminal, or — as one switch — every app-contributed tab. */
export type SidePanelWithholdable = ViewKind | 'terminal' | 'app'

// `SidePanel.tsx` renders the built-in group rows through it.
export { AddMenuItem }
