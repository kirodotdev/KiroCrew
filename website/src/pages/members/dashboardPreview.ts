/**
 * The Members page query that opens a crewmate's Dashboard tab on its STAGED page.
 *
 * `dashboard_preview` stages a page and hands the agent a link to show the person:
 * `/members?member=<exact crew name>&dashboard=preview`. The page opens that
 * crewmate, opens the side panel on the Dashboard tab, and the tab reads the staged
 * page instead of the live record, under a band that says nothing has changed yet.
 *
 * The two literals are mirrored by `PREVIEW_PAGE_PARAM` / `PREVIEW_PAGE_VALUE` in
 * `src/kiro_crew/dashboard_templates/instance.py`, which builds the link;
 * `test_dashboard_tools.py` pins the pair against this file.
 */
export const DASHBOARD_PREVIEW_PARAM = 'dashboard'
export const DASHBOARD_PREVIEW_VALUE = 'preview'

/** Whether this URL asks for the staged page. Any other value is the live page. */
export function isDashboardPreview(params: URLSearchParams): boolean {
  return params.get(DASHBOARD_PREVIEW_PARAM) === DASHBOARD_PREVIEW_VALUE
}

/** *params* without the staged-page request, once that page is gone (kept, applied
 *  by the agent, discarded or expired), so the tab reads the live page. The same
 *  object when it carries no request, so a caller can skip a no-op navigation. */
export function withoutDashboardPreview(params: URLSearchParams): URLSearchParams {
  if (!isDashboardPreview(params)) return params
  const next = new URLSearchParams(params)
  next.delete(DASHBOARD_PREVIEW_PARAM)
  return next
}
