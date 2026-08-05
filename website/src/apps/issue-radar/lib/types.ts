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
 * request list + detail split, or the settings page. Each corresponds to one
 * left-rail accordion section. */
export type MainView = 'dashboard' | 'issues' | 'pulls' | 'settings'

/** Which left-rail accordion section is expanded (the others collapse to their
 * title bar). Follows MainView by default; a header click overrides. */
export type ExpandedSection = 'dashboards' | 'filters' | 'pulls' | 'settings'

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
