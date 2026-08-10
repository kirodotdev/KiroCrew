import type { SourceProvider } from '../api'

// Shared Issue Radar UI types. Kept dependency-free so every module (context,
// components, views) can import from here without creating import cycles.

/** The repo the workspace is pointed at.
 *
 * `provider`/`host` are part of the IDENTITY, not decoration: they ride on every
 * request, and a `group/project` path names a different project on gitlab.com
 * than on a self-managed instance. They are optional so a value persisted before
 * GitLab support still loads — absent means public GitHub.
 *
 * Structurally a `RepoRef` (see `api.ts`), so it can be passed straight to any
 * API call without rebuilding it. */
export interface ActiveRepo {
  owner: string
  repo: string
  provider?: SourceProvider
  host?: string
}

/** Sort fields the issue list supports. Exported as a list so persisted state
 * can be validated at runtime (a key removed since it was persisted must not
 * survive a reload). */
export const SORT_KEYS = ['number', 'updated'] as const
export type SortKey = (typeof SORT_KEYS)[number]
export type SortDir = 'asc' | 'desc'

/** Sort fields the pull-request list supports. Same shape as ``SortKey``. */
export type PrSortKey = 'number' | 'updated'

/** Which full-page dashboard is showing in the main area. Extend this list
 * (plus the registry in views/registry.tsx) to add a new dashboard — no other
 * shared file needs to change, so views can be built by separate agents. The
 * list is exported so persisted state can be validated at runtime (a tab that
 * was removed since it was persisted must not survive a reload). */
export const DASHBOARD_TABS = ['overview', 'tagging'] as const
export type DashboardTab = (typeof DASHBOARD_TABS)[number]

/** Main-area mode: a dashboard page, the issue list + detail split, the pull-
 * request list + detail split, the crew list + crew page split, or the settings
 * page. Each corresponds to one left-rail accordion section.
 *
 * `crews` is a MainView rather than a DashboardTab because a dashboard renders
 * full-width with no list column, and the crews surface needs the list + main
 * split (roster in column 2, the selected crew's page in column 3). */
export type MainView = 'dashboard' | 'issues' | 'pulls' | 'crews' | 'settings'

/** Which left-rail accordion section is expanded (the others collapse to their
 * title bar). Follows MainView by default; a header click overrides. */
export type ExpandedSection = 'dashboards' | 'filters' | 'pulls' | 'crews' | 'settings'

/** What the crews main area is showing: one crew's own page, or nothing yet.
 * The column-2 crew list drives this — each crew row sets `{kind:'crew'}` with
 * that crew's id. `{kind:'none'}` is the state before a roster has loaded, and on
 * a repo with no crews at all; `context.tsx` opens the first crew as soon as one
 * exists, so it is never a page a user navigates TO. */
export type CrewView = { kind: 'none' } | { kind: 'crew'; id: string }

/** The `kind` discriminants, as a runtime list, so a persisted `CrewView` can be
 * validated on reload the way `SORT_KEYS` validates a persisted sort key. A
 * structurally valid kind is not enough on its own: `{kind:'crew'}` also carries
 * an id, and a crew that has since been retired (or belongs to another repo) must
 * not survive either — see `context.tsx`, which re-points the selection once the
 * crew list has loaded without it. */
export const CREW_VIEW_KINDS = ['none', 'crew'] as const

/** Chip filters over the crew roster. Independent predicates, NOT a partition:
 * the backend's own tallies are allowed to sum past the crew count (a paused crew
 * holding in-flight work counts in two), so nothing here should treat them as
 * slices of a whole. */
export const CREW_FILTERS = ['all', 'working', 'paused'] as const
export type CrewFilter = (typeof CREW_FILTERS)[number]

/** Sort fields offered over the crew roster.
 *
 * Deliberately only three, because `GET /crews` answers with crew RECORDS plus
 * repo-wide tallies and carries no work items: a "busiest" or "least recently
 * active" sort would need one request per crew, or a per-crew summary the payload
 * does not have. These three are answerable from a record alone — `status` is
 * derived by the route and already on it. */
export const CREW_SORT_KEYS = ['status', 'name', 'created'] as const
export type CrewSortKey = (typeof CREW_SORT_KEYS)[number]

/** Sub-sections of the General settings page the rail nav can jump to. */
export type GeneralAnchor = 'account' | 'repos'

/** What the Settings main area is showing: the shared "General" page (account +
 * connected-repo list), or one specific repo's settings page. The rail's
 * Settings section drives this — General items set `{kind:'general'}`, and each
 * connected repo gets its own `{kind:'repo'}` page. */
export type SettingsTarget =
  | { kind: 'general'; anchor?: GeneralAnchor }
  // Carries the provider identity for the same reason `ActiveRepo` does: the
  // settings pane reads and writes THIS repo's settings, and owner/repo alone
  // would address the wrong one on a mixed install.
  | { kind: 'repo'; owner: string; repo: string; provider?: SourceProvider; host?: string }

export type StateFilter = 'open' | 'closed'

/** Pull-request state filter. ``merged`` and ``closed`` both fetch the closed
 * set from GitHub; the frontend splits them on ``merged_at`` (merged = has a
 * merge timestamp; closed = closed WITHOUT being merged). */
export type PrStateFilter = 'open' | 'closed' | 'merged'
