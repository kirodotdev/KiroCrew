/** Ask the conductor Board panel to show its Workers list instead of Status.
 *  The panel keeps that choice in its own state, so a caller outside it (the
 *  status line under a conductor's reply) leaves the request here: a panel
 *  already on screen hears it as an event, and one that mounts later takes it
 *  as its first view. */
export type CrewPanelView = 'status' | 'list'

export const CREW_PANEL_VIEW_EVENT = 'mc-crew-panel-view'

const pending = new Map<string, CrewPanelView>()

/** Show *root*'s Workers list the next time its Board panel is on screen. */
export function requestCrewPanelList(root: string): void {
  pending.set(root, 'list')
  window.dispatchEvent(new CustomEvent<string>(CREW_PANEL_VIEW_EVENT, { detail: root }))
}

/** The view *root*'s panel was asked to show, once: the request is consumed. */
export function takeCrewPanelView(root: string): CrewPanelView | undefined {
  const view = pending.get(root)
  pending.delete(root)
  return view
}
