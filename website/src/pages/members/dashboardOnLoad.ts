/**
 * Which crewmates have already had their side panel put on the Dashboard tab in
 * THIS page load.
 *
 * The panel strip is persisted per slot (`usePanelTabs`), so a reload used to
 * reopen whatever tab was focused last -- Files, a terminal, a view. The first
 * time a crewmate's chat is shown after a page load (a refresh included), its
 * Dashboard tab opens instead: that tab is the crewmate's own page, or the
 * suggestions page when no page is adopted.
 *
 * Module-level on purpose: it empties on a reload and nowhere else, so moving
 * around the app and coming back keeps the tab the person picked. Keyed on the
 * crewmate ALONE, and claimed only once its thread's slot is confirmed (the
 * caller passes no slot before that): the strip re-buckets when the slot
 * confirms, and that confirmed bucket is the one a reload restores. One claim
 * per crewmate, so a second thread of the same crewmate, or the slot
 * confirming after a tab was picked, never forces Dashboard again.
 */
const opened = new Set<string>()

/** True the first time a crewmate with a confirmed slot is seen in this page
 *  load. A blank `slot` (not confirmed yet) never claims. */
export function claimDashboardOnLoad(member: string, slot: string): boolean {
  if (!member || !slot) return false
  if (opened.has(member)) return false
  opened.add(member)
  return true
}

/** Test seam: a fresh page load. */
export function resetDashboardOnLoad(): void {
  opened.clear()
}
