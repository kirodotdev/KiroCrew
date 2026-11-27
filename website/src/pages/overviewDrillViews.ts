/** Settings > Overview's drill-in views (`?view=`). Each one draws its own
 *  "‹ Overview" back row and title, so Settings leaves out the tab's own title
 *  and back bar while one is open. */
export const OVERVIEW_DRILL_VIEWS = ['usage', 'wakatime'] as const
export type OverviewDrillView = (typeof OVERVIEW_DRILL_VIEWS)[number]

export function isOverviewDrillView(view: string | null): view is OverviewDrillView {
  return (OVERVIEW_DRILL_VIEWS as readonly string[]).includes(view ?? '')
}
