/**
 * The ChatEmbed composer's picker source: the `/` command list and the `@`
 * file search, read through the app's scoped client.
 *
 * A read is in the source only when the host app's declared API paths cover
 * its endpoint, so a picker the app was not granted is hidden rather than
 * opened on a request the scoped client would refuse. Nothing is granted by
 * default, and this module never widens what an app may call.
 *
 * Left out on purpose:
 * - `./` path completion: it resolves against a project directory, and the
 *   embed has none to name.
 * - `$` skills: the listing is resolved against the chat's session key, which
 *   the scoped client does not let an app choose.
 */
import { useContext, useMemo } from 'react'
import { AppSdkContext, apiPathAllowed, type AppApi } from './scopedApi'
import { withDeadline } from '../lib/withDeadline'
import { SLASH_COMMANDS_TIMEOUT_MS } from '../api/client/chatSlotSettings'
import { FILE_SEARCH_TIMEOUT_MS } from '../api/client/files'
import type { ComposerPickerSource, FileSearchResponse, SlashCommand } from '../chat-core/composer/pickerSource'

/** The command list behind the `/` menu. A host opts in by declaring it. */
export const EMBED_SLASH_COMMANDS_PATH = '/api/slash-commands'
/** The name search behind the `@` menu. A host opts in by declaring it. */
export const EMBED_FILE_SEARCH_PATH = '/api/file-search'

const NO_PATHS: readonly string[] = []

/** Build the source for these declared paths. Exported for tests. */
export function embedPickerSource(api: AppApi, appName: string, allowedPaths: readonly string[]): ComposerPickerSource {
  // Scoped by app, so an embed never reads the dashboard's cached lists (or
  // another app's) under the same key.
  const source: ComposerPickerSource = { scope: `app:${appName}` }
  if (apiPathAllowed(allowedPaths, EMBED_SLASH_COMMANDS_PATH)) {
    // Same bound as the dashboard's read: the menu holds Enter while it loads.
    source.slashCommands = signal => withDeadline(SLASH_COMMANDS_TIMEOUT_MS, signal, s =>
      api.get<SlashCommand[]>(EMBED_SLASH_COMMANDS_PATH, { signal: s }))
  }
  if (apiPathAllowed(allowedPaths, EMBED_FILE_SEARCH_PATH)) {
    source.fileSearch = (q, project, signal) => {
      const p = new URLSearchParams({ q })
      if (project) p.set('project', project)
      return withDeadline(FILE_SEARCH_TIMEOUT_MS, signal, s =>
        api.get<FileSearchResponse>(`${EMBED_FILE_SEARCH_PATH}?${p}`, { signal: s }))
    }
  }
  return source
}

/** The embed's picker source, rebuilt only when the grant or the client changes. */
export function useEmbedPickerSource(api: AppApi, appName: string): ComposerPickerSource {
  // Read off the context directly (not through useAppInfo, which throws
  // without a provider) so a surface with no provider fails CLOSED: no
  // declared paths, no pickers.
  const allowedPaths = useContext(AppSdkContext)?.info.permissions.api ?? NO_PATHS
  return useMemo(() => embedPickerSource(api, appName, allowedPaths), [api, appName, allowedPaths])
}
