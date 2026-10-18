/**
 * The dashboard's picker source: the composer menus read through its own api
 * client, with the query keys the rest of the dashboard already invalidates
 * (`scope` is empty). It serves every composer that mounts no
 * `ComposerPickerSourceProvider` -- the main chat, the panes, the side chat --
 * so their menus behave exactly as before the source seam existed.
 *
 * Each member calls `api` at call time, not at module load, so a test that
 * stubs the api client is observed.
 */
import { api } from '../api/client'
import { useComposerPickerSource, type ComposerPickerSource, type FileSearchResponse, type SlashCommand } from '../chat-core/composer/pickerSource'

export const dashboardPickerSource: ComposerPickerSource = {
  scope: '',
  // A getter: a client without the endpoint leaves the menu on its offline
  // list instead of failing a fetch it could never make.
  get slashCommands() {
    return typeof api.slashCommands === 'function'
      ? (signal?: AbortSignal) => api.slashCommands(signal) as Promise<SlashCommand[]>
      : undefined
  },
  fileSearch: (q, project, signal) => api.fileSearch(q, project, signal) as Promise<FileSearchResponse>,
  pathComplete: (project, dir, q, signal) => api.pathComplete(project, dir, q, signal),
  skills: (slotKey, agent, signal) => api.skills(slotKey, agent, signal),
}

/** The host's source, or the dashboard's when no provider sits above. */
export function usePickerSource(): ComposerPickerSource {
  return useComposerPickerSource() ?? dashboardPickerSource
}
