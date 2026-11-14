import { MEMBER_DASHBOARD_QUERY_PREFIX, SESSION_DASHBOARD_QUERY_PREFIX } from '../../hooks/useWebSocket'

/**
 * The dashboard read's cache key, in its own module so a reader other than the
 * Dashboard tab (the chat's page colours) can share the tab's exact cache entry
 * without importing the tab's component module.
 */

/** Whose dashboard this frame reads: a crewmate's, or one ROOT session's own page. */
export type DashboardTarget =
  | { kind: 'member'; slug: string; member: string }
  | { kind: 'session'; slot: string }

/** The query key a target's read is cached under. The first segment is the prefix
 *  `handleDashboardMoved` invalidates by, so a frame naming only a slug or a slot
 *  reaches the read. */
export function dashboardQueryKey(target: DashboardTarget): readonly string[] {
  return target.kind === 'member'
    ? [MEMBER_DASHBOARD_QUERY_PREFIX, target.slug, target.member]
    : [SESSION_DASHBOARD_QUERY_PREFIX, target.slot]
}
