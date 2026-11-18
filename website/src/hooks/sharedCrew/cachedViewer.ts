/**
 * Every principal id that names the verified viewer, as this tab last read the
 * Crew's mode, for code that runs outside React (a close prompt raised from a
 * keyboard chord or a menu). The owner is one person under several ids (a
 * desktop-app token, a startup link, a tailnet login), so the server lists
 * them all in `viewer_ids`. It reads the cache the session list and the
 * composer already fill; it never fetches, so it is empty until the mode has
 * answered, and an empty set means "unknown".
 */
import { queryClient } from '../../api/queryClient'
import type { CrewModeResponse } from '../../api/sharedCrewMode'
import { isUnknownPrincipal } from '../../api/sharedCrewUi'
import { sharedCrewKeys } from './sharedCrewSocket'

export function cachedViewerIds(): string[] {
  const mode = queryClient.getQueryData<CrewModeResponse>(sharedCrewKeys.mode)
  const viewer = mode?.viewer
  if (!viewer || isUnknownPrincipal(viewer)) return []
  return mode.viewer_ids && mode.viewer_ids.length > 0 ? mode.viewer_ids : [viewer.id]
}
