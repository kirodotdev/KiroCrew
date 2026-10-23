/**
 * Attachments for the ChatEmbed composer: paste, drop and a file picker stage
 * files through the gateway's composer upload (`POST /api/upload/file`), and
 * the staged paths ride the send the same way the main chat's do (image lines
 * plus `meta.files`, built by chat-core's `buildOutgoingTurn`).
 *
 * Upload, staging and send-time recovery live in chat-core's store-free
 * `useComposerAttachments`. This adapter supplies only what is the app's:
 *
 * - the grant. The upload writes to the gateway's disk, so `granted` is true
 *   only when the host's declared API paths cover the upload path. Nothing
 *   grants it by default. Without the grant the attach control is hidden, and
 *   a pasted or dropped file shows the refusal notice instead of failing
 *   silently. A host that hides attachments (`hide={['attach']}`) gets the
 *   same ungranted behaviour whatever it declares: hiding never adds a grant,
 *   and a paste or drop must not stage a file the host chose not to offer;
 * - the transport: the app's scoped client, so the same declared-path check
 *   that gates every other call gates the upload too.
 */
import { useCallback, useContext } from 'react'
import type { AppApi } from './scopedApi'
import { AppSdkContext, apiPathAllowed } from './scopedApi'
import { COMPOSER_UPLOAD_PATH } from '../utils/composerUpload'
import { useComposerAttachments, type ComposerAttachments } from '../chat-core/composer/useComposerAttachments'
import { i18nT } from '../i18n/t'

/** The composer upload endpoint. A host opts in by declaring it. */
export const EMBED_UPLOAD_PATH = COMPOSER_UPLOAD_PATH

export type EmbedAttachments = ComposerAttachments

const NO_PATHS: readonly string[] = []

export function useEmbedAttachments(api: AppApi, slotKey: string, hidden = false): EmbedAttachments {
  // The host's declared API paths. Read off the context directly (not through
  // useAppInfo, which throws without a provider) so a surface with no provider
  // fails CLOSED: no declared paths, no attach control.
  const allowedPaths = useContext(AppSdkContext)?.info.permissions.api ?? NO_PATHS
  const granted = !hidden && apiPathAllowed(allowedPaths, EMBED_UPLOAD_PATH)
  // `raw` forwards the multipart body untouched (no JSON content type),
  // through the same declared-path check as every other scoped call. It
  // throws on an HTTP failure; the shared hook reads the refusal.
  const post = useCallback(
    (body: FormData, signal: AbortSignal) => api.raw(EMBED_UPLOAD_PATH, { method: 'POST', body, signal }),
    [api],
  )
  // Name the dashboard page that accepts files by its own rail label
  // (`nav.sessions`, the `/chat` surface), so the notice and the sidebar agree
  // in every language. Text only: a link would navigate away from this
  // embed's unsaved draft.
  const deniedNotice = useCallback(
    () => i18nT('appSdk.chatEmbed.attachments_not_allowed', { page: i18nT('nav.sessions') }),
    [],
  )
  return useComposerAttachments({ resetKey: slotKey, granted, post, deniedNotice })
}
