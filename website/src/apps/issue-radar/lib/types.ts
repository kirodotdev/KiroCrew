// Shared Issue Radar UI types. Kept dependency-free so every module (context,
// components, views) can import from here without creating import cycles.

export interface ActiveRepo {
  owner: string
  repo: string
}

export type SortKey = 'number' | 'updated' | 'ranking'
export type SortDir = 'asc' | 'desc'

/** Which full-page dashboard is showing in the main area. Extend this union
 * (plus the registry in views/registry.tsx) to add a new dashboard — no other
 * shared file needs to change, so views can be built by separate agents. */
export type DashboardTab = 'overview' | 'tagging' | 'ranking' | 'insights' | 'duplicates'

/** Main-area mode: a dashboard page, the issue list + detail split, or the
 * settings page. Each corresponds to one left-rail accordion section. */
export type MainView = 'dashboard' | 'issues' | 'settings'

/** Which left-rail accordion section is expanded (the others collapse to their
 * title bar). Follows MainView by default; a header click overrides. */
export type ExpandedSection = 'dashboards' | 'filters' | 'settings'

/** Sub-sections of the General settings page the rail nav can jump to. */
export type GeneralAnchor = 'account' | 'repos'

/** What the Settings main area is showing: the shared "General" page (account +
 * connected-repo list), or one specific repo's settings page. The rail's
 * Settings section drives this — General items set `{kind:'general'}`, and each
 * connected repo gets its own `{kind:'repo'}` page. */
export type SettingsTarget =
  | { kind: 'general'; anchor?: GeneralAnchor }
  | { kind: 'repo'; owner: string; repo: string }

export type StateFilter = 'open' | 'closed'
