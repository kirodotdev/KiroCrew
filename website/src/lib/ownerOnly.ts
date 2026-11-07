/**
 * What a Shared Crew guest cannot open. The server refuses these routes (see
 * `kiro_crew.shared_crew.guest_scope`); this list only keeps the dashboard from
 * advertising them.
 */
export const GUEST_HIDDEN_NAV_IDS: ReadonlySet<string> = new Set(['apps', 'capabilities', 'webhooks', 'settings'])

const OWNER_ONLY_PATHS = ['/apps', '/capabilities', '/webhooks', '/settings', '/hooks', '/connections', '/instances', '/deploy']

/** A schedule a guest may not edit, run, delete or start again: it runs without a person approving each step. */
export function isOwnerOnlySchedule(job: { approval_mode?: string | null; script?: string | null; command?: string | null }): boolean {
  return job.approval_mode === 'auto' || !!job.script || !!job.command
}

/** Whether a guest has no use for this nav row. */
export function hiddenForGuest(navId: string): boolean {
  return GUEST_HIDDEN_NAV_IDS.has(navId)
}

/** Whether `pathname` is a page a guest cannot open. */
export function isOwnerOnlyPath(pathname: string): boolean {
  return OWNER_ONLY_PATHS.some(p => pathname === p || pathname.startsWith(p + '/'))
}
